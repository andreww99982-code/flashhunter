"""Управление стратегиями: список, включение/выключение, параметры, ручной триггер."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from flashhunter.api.deps import CriticalUser, CurrentUser, SessionDep, service, write_audit
from flashhunter.db.models import StrategyConfig

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyUpdate(BaseModel):
    """Изменение состояния стратегии из UI."""

    enabled: bool | None = None
    params: dict[str, Any] | None = None
    chains: list[int] | None = None


@router.get("")
async def list_strategies(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Список стратегий: конфигурация из БД + живая статистика по каждой."""
    rows = (await db.execute(select(StrategyConfig))).scalars().all()
    svc = service()
    live = {s.name: s for s in svc.strategies}
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "kind": row.kind.value,
                "enabled": row.enabled,
                "chains": row.chains,
                "params": row.params,
                "running": row.name in live,
                "latency_ms": round(svc.stats.avg_latency_ms, 2) if row.name in live else None,
            }
            for row in rows
        ]
    }


@router.patch("/{name}")
async def update_strategy(
    name: str, payload: StrategyUpdate, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Обновляет параметры стратегии. Включение/выключение применяется на лету."""
    result = await db.execute(select(StrategyConfig).where(StrategyConfig.name == name))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"strategy {name} not found")

    if payload.params is not None:
        row.params = {**(row.params or {}), **payload.params}
    if payload.chains is not None:
        row.chains = payload.chains
    if payload.enabled is not None:
        row.enabled = payload.enabled

    await write_audit(db, request, user.username, "strategy_update", {"name": name, "payload": payload.model_dump(exclude_none=True)})

    # перезагрузка состава стратегий оркестратора без перезапуска процесса
    svc = service()
    await svc._load_strategies()
    return {"ok": True, "name": name, "enabled": row.enabled, "params": row.params, "chains": row.chains}


@router.post("/{name}/toggle")
async def toggle_strategy(name: str, request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Быстрое переключение вкл/выкл (используется тумблером в UI)."""
    result = await db.execute(select(StrategyConfig).where(StrategyConfig.name == name))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"strategy {name} not found")
    row.enabled = not row.enabled
    await write_audit(db, request, user.username, "strategy_toggle", {"name": name, "enabled": row.enabled})
    svc = service()
    await svc._load_strategies()
    return {"ok": True, "name": name, "enabled": row.enabled}
