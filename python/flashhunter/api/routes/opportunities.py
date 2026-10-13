"""Live-таблица возможностей: фильтры, ручное исполнение, статистика по стратегиям."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from flashhunter.api.deps import CriticalUser, CurrentUser, Page, SessionDep, service, write_audit
from flashhunter.db.models import Opportunity, OpportunityStatus
from flashhunter.db.session import session_scope
from flashhunter.pnl.tracker import PnlTracker
from sqlalchemy import desc, func, select

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


class ManualExecuteRequest(BaseModel):
    """Параметры ручного исполнения из UI."""

    uid: str
    dry_run_override: bool | None = None


@router.get("")
async def list_opportunities(
    user: CurrentUser,
    db: SessionDep,
    page: Page,
    strategy: str | None = Query(default=None),
    chain_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    min_profit: float = Query(default=0.0, ge=0),
    live_only: bool = Query(default=False, description="Только из памяти сервиса (последние блоки)"),
) -> dict[str, Any]:
    """Список возможностей: из памяти оркестратора (live) или из БД (история)."""
    svc = service()
    if live_only:
        rows = [
            row
            for row in svc.recent_opportunities
            if (strategy is None or row["strategy"] == strategy)
            and (chain_id is None or row["chain_id"] == chain_id)
            and row["net_profit_usd"] >= min_profit
        ]
        return {"items": rows[page.offset : page.offset + page.limit], "total": len(rows), "live": True}

    stmt = select(Opportunity)
    if strategy:
        stmt = stmt.where(Opportunity.strategy == strategy)
    if chain_id:
        stmt = stmt.where(Opportunity.chain_id == chain_id)
    if status_filter:
        stmt = stmt.where(Opportunity.status == OpportunityStatus(status_filter))
    if min_profit > 0:
        stmt = stmt.where(Opportunity.net_profit_usd >= min_profit)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(desc(Opportunity.ts)).offset(page.offset).limit(page.limit))
    ).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "chain_id": row.chain_id,
                "strategy": row.strategy,
                "token_in": row.token_in,
                "token_out": row.token_out,
                "dex_buy": row.dex_buy,
                "dex_sell": row.dex_sell,
                "size_usd": float(row.size_usd),
                "gross_profit_usd": float(row.gross_profit_usd),
                "gas_usd": float(row.gas_usd),
                "flash_fee_usd": float(row.flash_fee_usd),
                "net_profit_usd": float(row.net_profit_usd),
                "roi_pct": row.roi_pct,
                "confidence": row.confidence,
                "status": row.status.value,
                "block_number": row.block_number,
                "path": row.path,
            }
            for row in rows
        ],
        "total": int(total or 0),
        "live": False,
    }


@router.get("/stats")
async def opportunity_stats(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Агрегаты по стратегиям: сколько найдено, средняя прибыль, конверсия в сделки."""
    rows = (
        await db.execute(
            select(
                Opportunity.strategy,
                func.count(Opportunity.id),
                func.coalesce(func.avg(Opportunity.net_profit_usd), 0),
                func.coalesce(func.max(Opportunity.net_profit_usd), 0),
                func.coalesce(func.sum(Opportunity.size_usd), 0),
            ).group_by(Opportunity.strategy)
        )
    ).all()
    tracker = PnlTracker(db)
    by_strategy = {item["key"]: item for item in await tracker.top_by("strategy", "all", 20)}
    return {
        "strategies": [
            {
                "strategy": strategy,
                "found": int(count),
                "avg_net_usd": float(avg_net),
                "best_net_usd": float(best_net),
                "total_size_usd": float(total_size),
                "executed": by_strategy.get(strategy, {}).get("trades", 0),
                "win_rate_pct": by_strategy.get(strategy, {}).get("win_rate_pct", 0.0),
            }
            for strategy, count, avg_net, best_net, total_size in rows
        ]
    }


@router.post("/execute")
async def execute_manually(
    payload: ManualExecuteRequest,
    request: Request,
    user: CriticalUser,  # критическое действие: требует TOTP, если он включён
    db: SessionDep,
) -> dict[str, Any]:
    """Ручное исполнение возможности из live-буфера (кнопка Execute в UI).

    Возможность исполняется по последнему снимку рынка: между находкой и нажатием
    кнопки пулы могли измениться, поэтому срабатывает обычный контур — симуляция
    и риск-проверки — и сделка может быть отклонена (это ожидаемое поведение).
    """
    svc = service()
    target = next((o for o in svc.recent_opportunity_objects if o.uid == payload.uid), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opportunity not found in live buffer")
    if svc.last_snapshot is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no market snapshot available yet")
    if not any(s.name == target.strategy and s.enabled for s in svc.strategies):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"strategy {target.strategy} is not enabled"
        )

    await write_audit(db, request, user.username, "manual_execute", {"uid": payload.uid, "strategy": target.strategy})
    async with session_scope() as session:
        await svc.executor.persist_opportunity(session, target, OpportunityStatus.detected)
        result = await svc.executor.execute(target, svc.last_snapshot, session)

    await svc._publish({"type": "execution", "data": {**result.as_dict(), "manual": True, "by": user.username}})
    return {"ok": result.accepted, "result": result.as_dict()}
