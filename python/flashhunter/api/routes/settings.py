"""Настройки: RPC/API-ключи, риск-лимиты, kill-switch, переключение dry-run → live."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from flashhunter.api.deps import CriticalUser, CurrentUser, SessionDep, service, write_audit
from flashhunter.db.models import ApiKeyEncrypted, RiskLimit, Setting
from flashhunter.db.models import utcnow
from flashhunter.strategies.service import STRATEGY_CLASSES

router = APIRouter(prefix="/settings", tags=["settings"])

DRY_RUN_MIN_HOURS = 24  # ТЗ: обязательный dry-run 24 часа до переключения в live


class RiskLimitUpdate(BaseModel):
    """Изменение риск-лимита."""

    scope: str = Field(pattern="^(global|chain|token|protocol)$")
    key: str = Field(min_length=1, max_length=64)
    value: dict[str, Any]


class KillSwitchRequest(BaseModel):
    """Управление kill-switch."""

    engage: bool
    reason: str = Field(default="manual", max_length=200)


class ApiKeyCreate(BaseModel):
    """Добавление ключа внешнего сервиса (Etherscan, Tenderly, RPC-провайдеры)."""

    provider: str = Field(min_length=1, max_length=32)
    api_key: str = Field(min_length=8, max_length=512)


class ModeSwitchRequest(BaseModel):
    """Переключение режима dry-run → live."""

    live: bool
    confirmation: str = Field(default="", description="Для перехода в live нужно ввести слово LIVE")
    totp: str | None = None


@router.get("")
async def get_settings_view(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Текущие настройки: лимиты, режим, ключи (маскированные), состояние рисков."""
    limits = (await db.execute(select(RiskLimit))).scalars().all()
    settings_rows = (await db.execute(select(Setting))).scalars().all()
    keys = (await db.execute(select(ApiKeyEncrypted))).scalars().all()
    svc = service()
    return {
        "risk": svc.risk.snapshot_state(),
        "limits": [
            {"id": r.id, "scope": r.scope, "key": r.key, "value": r.value,
             "updated_at": r.updated_at.isoformat()} for r in limits
        ],
        "settings": {r.key: r.value for r in settings_rows},
        "api_keys": [
            {"id": k.id, "provider": k.provider, "masked": "••••" + k.encrypted_key[-4:],
             "created_at": k.created_at.isoformat()} for k in keys
        ],
        "strategies_available": sorted(STRATEGY_CLASSES),
        # Боевой контур исполнения: без секретов, только то, что нужно оператору
        "execution": svc.state()["execution"],
    }


@router.put("/limits")
async def upsert_limit(payload: RiskLimitUpdate, request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Создаёт/обновляет риск-лимит и немедленно применяет его в риск-движке."""
    result = await db.execute(
        select(RiskLimit).where(RiskLimit.scope == payload.scope, RiskLimit.key == payload.key)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = RiskLimit(scope=payload.scope, key=payload.key, value=payload.value)
        db.add(row)
    else:
        row.value = payload.value
        row.updated_at = utcnow()
    await write_audit(db, request, user.username, "limit_update",
                      {"scope": payload.scope, "key": payload.key, "value": payload.value})
    _apply_limit_to_engine(payload.scope, payload.key, payload.value)
    return {"ok": True}


def _apply_limit_to_engine(scope: str, key: str, value: dict[str, Any]) -> None:
    """Применяет лимит к живому риск-движку (без перезапуска сервисов)."""
    from flashhunter.risk.engine import get_risk_engine

    engine = get_risk_engine()
    limit = value.get("limit") or value.get("max_exposure_usd")
    if limit is None:
        return
    from decimal import Decimal

    dec = Decimal(str(limit))
    mapping = {
        ("global", "daily_loss_limit_usd"): "daily_loss_limit",
        ("global", "hourly_loss_limit_usd"): "hourly_loss_limit",
        ("global", "gas_ceiling_gwei"): "gas_ceiling_gwei",
        ("global", "max_slippage_bps"): "slippage_ceiling_bps",
        ("global", "latency_p99_limit_ms"): "latency_p99_limit_ms",
        ("global", "connection_loss_sec"): "connection_loss_sec",
        ("global", "revert_rate_limit_pct"): "revert_rate_limit",
    }
    attr = mapping.get((scope, key))
    if attr:
        current = getattr(engine, attr)
        setattr(engine, attr, dec if isinstance(current, Decimal) else type(current)(dec))
    elif scope == "chain":
        engine.max_exposure_chain = dec
    elif scope == "token":
        engine.max_exposure_token = dec


@router.post("/kill-switch")
async def kill_switch(payload: KillSwitchRequest, request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Взводит/снимает kill-switch (взводится мгновенно, снимается только вручную)."""
    svc = service()
    if payload.engage:
        svc.risk.engage_kill_switch(payload.reason or "manual")
        svc.pause()
    else:
        svc.risk.release_kill_switch("manual release from UI")
        svc.resume()
    await write_audit(db, request, user.username, "kill_switch", {"engage": payload.engage, "reason": payload.reason})
    setting = await db.get(Setting, "kill_switch")
    if setting is not None:
        setting.value = {"active": payload.engage, "reason": payload.reason, "changed_at": utcnow().isoformat()}
    await svc._publish({"type": "kill_switch", "data": {"engage": payload.engage, "reason": payload.reason}})
    return {"ok": True, "kill_switch": payload.engage, "kill_reason": payload.reason}


@router.post("/mode")
async def switch_mode(payload: ModeSwitchRequest, request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Переключает dry-run ↔ live. В live требуется подтверждение словом LIVE и dry-run ≥ 24 ч."""
    svc = service()
    if not payload.live:
        svc.risk.dry_run = True
        await write_audit(db, request, user.username, "mode_switch", {"live": False})
        await svc._publish({"type": "mode", "data": {"dry_run": True}})
        return {"ok": True, "dry_run": True}

    if payload.confirmation.strip().upper() != "LIVE":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "введите LIVE для подтверждения")

    started = await db.get(Setting, "dry_run_started_at")
    started_ts = (started.value or {}).get("ts") if started else None
    hours = 0.0
    if started_ts:
        from datetime import datetime

        try:
            hours = (utcnow() - datetime.fromisoformat(started_ts).replace(tzinfo=None)).total_seconds() / 3600
        except ValueError:
            hours = 0.0
    if hours < DRY_RUN_MIN_HOURS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"dry-run обязателен минимум {DRY_RUN_MIN_HOURS} ч (прошло {hours:.1f} ч)",
        )
    if not (svc.risk.kill_reason or not svc.risk.kill_switch):
        raise HTTPException(status.HTTP_409_CONFLICT, "сначала проверьте состояние kill-switch")

    svc.risk.dry_run = False
    setting = await db.get(Setting, "live_confirmed")
    if setting is not None:
        setting.value = {"confirmed": True, "confirmed_at": utcnow().isoformat()}
    await write_audit(db, request, user.username, "mode_switch", {"live": True})
    await svc._publish({"type": "mode", "data": {"dry_run": False}})
    return {"ok": True, "dry_run": False, "confirmed_at": utcnow().isoformat(), "dry_run_hours": round(hours, 2)}


@router.post("/api-keys")
async def add_api_key(
    payload: ApiKeyCreate, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Сохраняет API-ключ провайдера в зашифрованном виде (в prod — Vault transit).

    Ключ приходит телом запроса: query-параметр попал бы в логи прокси и историю браузера.
    """
    from flashhunter.config.secrets import encrypt_secret, mask_secret

    row = ApiKeyEncrypted(provider=payload.provider, encrypted_key=encrypt_secret(payload.api_key))
    db.add(row)
    await db.flush()
    await write_audit(db, request, user.username, "api_key_add", {"provider": payload.provider})
    return {"ok": True, "id": row.id, "provider": payload.provider, "masked": mask_secret(payload.api_key)}


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: int, request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Удаляет API-ключ."""
    row = await db.get(ApiKeyEncrypted, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    await db.delete(row)
    await write_audit(db, request, user.username, "api_key_delete", {"id": key_id})
    return {"ok": True}


@router.get("/ping")
async def ping() -> dict[str, float]:
    """Проверка живости API (без авторизации) — используется мониторингом."""
    return {"ts": time.time()}
