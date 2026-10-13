"""Настройки: RPC/API-ключи, риск-лимиты, kill-switch, переключение dry-run → live."""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any, Literal, get_args, get_origin

from eth_account import Account
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from flashhunter.api.auth import verify_password
from flashhunter.api.deps import CriticalUser, CurrentUser, SessionDep, service, write_audit
from flashhunter.config.panel_config import load_panel_settings, save_panel_settings
from flashhunter.config.secrets import decrypt_secret, encrypt_secret, mask_secret
from flashhunter.config.settings import Settings, get_settings
from flashhunter.db.models import ApiKeyEncrypted, RiskLimit, Setting, User, Wallet, utcnow
from flashhunter.strategies.service import STRATEGY_CLASSES

router = APIRouter(prefix="/settings", tags=["settings"])
log = logging.getLogger(__name__)

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


class RuntimeConfigUpdate(BaseModel):
    """Validated runtime configuration; secret values are encrypted at rest."""

    values: dict[str, Any]


def _config_secret(name: str, field: Any) -> bool:
    annotation = field.annotation
    return annotation is SecretStr or any(
        marker in name.lower()
        for marker in (
            "password",
            "secret",
            "private_key",
            "rpc_http",
            "rpc_ws",
            "rpc_fallback",
            "relay_url",
            "_dsn",
            "redis_url",
            "nats_url",
            "webhook",
        )
    )


def _config_kind(field: Any) -> str:
    annotation = field.annotation
    if annotation is bool:
        return "boolean"
    if annotation in (int, float, Decimal):
        return "number"
    if get_origin(annotation) is list:
        return "list"
    if get_origin(annotation) is Literal:
        return "select"
    return "text"


def _config_category(name: str) -> str:
    if name.startswith(("eth_", "arb_", "base_", "op_", "bsc_", "polygon_", "avalanche_", "live_")):
        return "Сети и RPC"
    if name.startswith(("strat_",)):
        return "Стратегии"
    if name.startswith(("exec_", "flashloan_", "liquidator_", "jit_", "keeper_")):
        return "Исполнение и контракты"
    if name.startswith(("risk_", "cold_wallet_", "sweep_")):
        return "Риск и кошельки"
    if name.startswith(("alert_", "telegram_", "discord_")):
        return "Уведомления"
    if name.startswith(("db_", "sqlite_", "postgres_", "clickhouse_", "redis_", "nats_")):
        return "Хранилища"
    if name.startswith(("api_", "jwt_", "admin_", "cors_", "session_")):
        return "API и безопасность"
    return "Общие"


def _field_value(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _config_secret_values(row: Setting | None) -> dict[str, Any]:
    if row is None:
        return {}
    values = dict(row.value or {})
    for name, value in list(values.items()):
        if isinstance(value, dict) and value.get("encrypted"):
            values[name] = decrypt_secret(value["encrypted"])
    return values


def _panel_config_values() -> dict[str, Any]:
    values = load_panel_settings()
    return {
        name: decrypt_secret(value["encrypted"])
        if isinstance(value, dict) and isinstance(value.get("encrypted"), str)
        else value
        for name, value in values.items()
    }


async def _upgrade_legacy_secrets(db: SessionDep, stored: dict[str, Any]) -> None:
    """Re-encrypt v1 secrets before a UI-driven JWT rotation changes legacy key derivation."""
    try:
        for name, value in stored.items():
            encrypted = value.get("encrypted") if isinstance(value, dict) else None
            if isinstance(encrypted, str) and encrypted.startswith("local:v1:"):
                value["encrypted"] = encrypt_secret(decrypt_secret(encrypted))
        wallets = (await db.execute(select(Wallet))).scalars().all()
        for wallet in wallets:
            if wallet.encrypted_key and wallet.encrypted_key.startswith("local:v1:"):
                wallet.encrypted_key = encrypt_secret(decrypt_secret(wallet.encrypted_key))
        api_keys = (await db.execute(select(ApiKeyEncrypted))).scalars().all()
        for api_key in api_keys:
            if api_key.encrypted_key.startswith("local:v1:"):
                api_key.encrypted_key = encrypt_secret(decrypt_secret(api_key.encrypted_key))
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cannot rotate JWT secret until existing encrypted credentials can be decrypted",
        ) from exc


def _settings_with_overrides(overrides: dict[str, Any]) -> Settings:
    current = get_settings()
    alias_values = {
        field.alias or name: _field_value(getattr(current, name))
        for name, field in Settings.model_fields.items()
    }
    for name, value in overrides.items():
        field = Settings.model_fields.get(name)
        if field is None:
            raise ValueError(f"unknown setting: {name}")
        alias_values[field.alias or name] = value
    return Settings.model_validate(alias_values)


async def load_runtime_config(db: Any) -> None:
    """Applies persisted panel configuration before services are constructed."""
    settings = get_settings()
    row = await db.get(Setting, "runtime_config")
    overrides = _config_secret_values(row) | _panel_config_values()
    selected = await db.get(Setting, "trading_wallet")
    if selected and (wallet_id := (selected.value or {}).get("wallet_id")):
        wallet = await db.get(Wallet, wallet_id)
        if wallet and wallet.encrypted_key:
            overrides["keeper_private_key"] = decrypt_secret(wallet.encrypted_key)
    if overrides:
        try:
            _apply_settings(settings, _settings_with_overrides(overrides))
        except (ValueError, TypeError):
            log.exception("Persisted settings are invalid; keeping environment configuration")


_CONFIG_EXCLUDED = {
    "admin_default_password",
    "mock_market_data",
}


@router.get("/config")
async def get_runtime_config(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Panel-visible settings schema and current values; secrets are never returned."""
    current = get_settings()
    row = await db.get(Setting, "runtime_config")
    overrides = dict(row.value or {}) if row else {}
    overrides.update(load_panel_settings())
    effective = current
    try:
        effective = _settings_with_overrides(_config_secret_values(row) | _panel_config_values())
    except (ValueError, TypeError):
        log.exception("Unable to load saved settings for display")
    fields = []
    for name, field in Settings.model_fields.items():
        if name in _CONFIG_EXCLUDED:
            continue
        secret = _config_secret(name, field)
        value = getattr(effective, name)
        masked = _field_value(value) if secret else None
        fields.append(
            {
                "name": name,
                "label": field.alias or name,
                "category": _config_category(name),
                "kind": "secret" if secret else _config_kind(field),
                "value": None if secret else _field_value(value),
                "configured": bool(masked) if secret else True,
                "masked": mask_secret(masked) if secret and masked else None,
                "choices": list(get_args(field.annotation)) if _config_kind(field) == "select" else [],
                "description": field.description or "",
            }
        )
    locked_fields = [
        {
            "name": name,
            "label": Settings.model_fields[name].alias or name,
            "category": _config_category(name),
            "reason": (
                "Отключено: система должна использовать только реальные рыночные данные."
                if name == "mock_market_data"
                else "Пароль учётной записи меняется через профиль пользователя."
            ),
        }
        for name in sorted(_CONFIG_EXCLUDED)
    ]
    return {"fields": fields, "locked_fields": locked_fields, "overrides": sorted(overrides)}


@router.put("/config")
async def update_runtime_config(
    payload: RuntimeConfigUpdate, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Validates and saves the submitted config; changed settings apply after restart."""
    if not payload.values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no settings provided")
    unknown = set(payload.values) - (set(Settings.model_fields) - _CONFIG_EXCLUDED)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unsupported settings: {', '.join(sorted(unknown))}",
        )
    try:
        candidate = _settings_with_overrides(payload.values)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if candidate.env == "prod":
        admins = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
        if not admins or any(
            admin.must_change_password or verify_password("admin", admin.password_hash)
            for admin in admins
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "change the default administrator password in the profile before enabling production mode",
            )

    row = await db.get(Setting, "runtime_config")
    stored = dict(row.value or {}) if row else {}
    if "jwt_secret" in payload.values and not os.environ.get("VAULT_ADDR"):
        await _upgrade_legacy_secrets(db, stored)
    panel_updates: dict[str, Any] = {}
    for name in payload.values:
        value = _field_value(getattr(candidate, name))
        encrypted_or_plain = (
            {"encrypted": encrypt_secret(str(value))}
            if _config_secret(name, Settings.model_fields[name])
            else value
        )
        stored[name] = encrypted_or_plain
        panel_updates[name] = encrypted_or_plain
    try:
        save_panel_settings(panel_updates)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "could not persist panel settings"
        ) from exc
    if row is None:
        row = Setting(key="runtime_config", value=stored)
        db.add(row)
    else:
        row.value = stored
    await write_audit(db, request, user.username, "runtime_config_update", {"fields": sorted(payload.values)})
    return {"ok": True, "restart_required": True, "updated": sorted(payload.values)}


@router.post("/trading-wallet/{wallet_id}")
async def set_trading_wallet(
    wallet_id: int, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Selects a hot wallet as the trade signer; encrypted key is only read at startup."""
    wallet = await db.get(Wallet, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "wallet not found")
    if not wallet.is_hot or not wallet.encrypted_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "trading wallet must be hot and have a private key"
        )
    if get_settings().live_chain_id not in (wallet.chain_scope or []):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "wallet is not enabled for the active chain"
        )
    try:
        private_key = decrypt_secret(wallet.encrypted_key)
        derived_address = Account.from_key(private_key).address
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "wallet private key is invalid") from exc
    if derived_address.lower() != wallet.address.lower():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "wallet address does not match its private key"
        )
    setting = await db.get(Setting, "trading_wallet")
    if setting is None:
        db.add(Setting(key="trading_wallet", value={"wallet_id": wallet.id}))
    else:
        setting.value = {"wallet_id": wallet.id}
    await write_audit(db, request, user.username, "trading_wallet_select", {"wallet_id": wallet.id})
    return {"ok": True, "wallet_id": wallet.id, "address": wallet.address, "restart_required": True}


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
