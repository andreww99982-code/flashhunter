"""Аналитика: win rate, ROI, Sharpe, drawdown, топ пар/релеев/стратегий, дневные агрегаты."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from flashhunter.api.deps import CurrentUser, SessionDep, service
from flashhunter.db.models import DailyStat, Trade, TradeStatus
from flashhunter.pnl.tracker import PnlTracker

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def summary(
    user: CurrentUser,
    db: SessionDep,
    period: str = Query(default="7d", pattern="^(24h|7d|30d|all)$"),
) -> dict[str, Any]:
    """Полная аналитика за период: сводка + разбивки + дневные агрегаты."""
    tracker = PnlTracker(db)
    pnl = (await tracker.summary(period)).as_dict()
    by_strategy = await tracker.top_by("strategy", period, 10)
    by_relay = await tracker.top_by("relay", period, 10)
    by_chain = await tracker.top_by("chain_id", period, 10)
    curve = await tracker.equity_curve(period, points=96)

    # топ торговых пар: группируем по стратегии+сети (пары токенов дают слишком мелкие группы)
    pair_rows = (
        await db.execute(
            select(
                Trade.chain_id,
                Trade.strategy,
                func.count(Trade.id),
                func.coalesce(func.sum(Trade.actual_profit_usd), 0),
            )
            .where(Trade.status == TradeStatus.success)
            .group_by(Trade.chain_id, Trade.strategy)
            .order_by(desc(func.sum(Trade.actual_profit_usd)))
            .limit(10)
        )
    ).all()

    daily = (
        await db.execute(
            select(DailyStat).order_by(desc(DailyStat.date)).limit(30)
        )
    ).scalars().all()

    state = service().state()
    return {
        "period": period,
        "pnl": pnl,
        "equity_curve": curve,
        "by_strategy": by_strategy,
        "by_relay": by_relay,
        "by_chain": by_chain,
        "top_pairs": [
            {"chain_id": chain, "strategy": strategy, "trades": int(count), "net_usd": float(net)}
            for chain, strategy, count, net in pair_rows
        ],
        "daily": [
            {
                "date": d.date,
                "chain_id": d.chain_id,
                "strategy": d.strategy,
                "net_usd": float(d.net_usd),
                "trades": d.trades,
                "wins": d.wins,
                "losses": d.losses,
            }
            for d in daily
        ],
        "relay_stats": state["relays"],
    }


@router.post("/rollup")
async def rollup(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Пересчитывает дневные агрегаты (вызывается по cron/в конце дня)."""
    tracker = PnlTracker(db)
    stats = await tracker.rollup_daily()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    stats += await tracker.rollup_daily(yesterday)
    return {"ok": True, "rows": len(stats)}
