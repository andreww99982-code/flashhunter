"""Дашборд: балансы, PnL по периодам, статус сервисов, kill-switch, кривая капитала."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from flashhunter.api.deps import CurrentUser, SessionDep, service
from flashhunter.db.models import ServiceHealth, Trade, TradeStatus, Wallet
from flashhunter.pnl.tracker import PnlTracker

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview")
async def overview(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Полная сводка для главного экрана (обновляется по WS и опросом)."""
    svc = service()
    tracker = PnlTracker(db)
    pnl = {period: (await tracker.summary(period)).as_dict() for period in ("24h", "7d", "30d", "all")}
    curve = await tracker.equity_curve("24h", points=48)

    wallets = (await db.execute(select(Wallet))).scalars().all()
    health = (await db.execute(select(ServiceHealth))).scalars().all()

    today_start = func.date(Trade.ts)
    trades_today = (
        await db.execute(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.success, today_start == func.date("now")
            )
        )
    ).scalar_one()

    state = svc.state()
    return {
        "pnl": pnl,
        "equity_curve": curve,
        "wallets": [
            {
                "id": w.id,
                "name": w.name,
                "address": w.address,
                "is_hot": w.is_hot,
                "balance_usd": float(w.balance_usd),
                "native_balance": float(w.native_balance),
                "chains": w.chain_scope,
            }
            for w in wallets
        ],
        "services": {
            "strategy_service": state["stats"],
            "market": state["market"],
            "strategies_enabled": [s["name"] for s in state["strategies"] if s["enabled"]],
            "relays": state["relays"],
            "health": [
                {"service": h.service, "status": h.status, "latency_ms": h.latency_ms,
                 "last_seen": h.last_seen.isoformat()}
                for h in health
            ],
        },
        "risk": state["risk"],
        "kill_switch": state["kill_switch"],
        "kill_reason": state["kill_reason"],
        "dry_run": state["dry_run"],
        "trades_today": int(trades_today or 0),
        "user": user.username,
    }
