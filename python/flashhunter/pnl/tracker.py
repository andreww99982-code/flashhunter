"""PnL-трекер: агрегация сделок, win rate, ROI, Sharpe, drawdown, топ-пары и релеи.

Работает поверх таблиц ``trades`` / ``daily_stats``. В cluster-режиме те же метрики
считаются в ClickHouse (materialized views), здесь — универсальная Python-реализация
для single-node SQLite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flashhunter.db.models import DailyStat, Trade, TradeStatus


@dataclass(slots=True)
class PnlSummary:
    """Сводка PnL за период."""

    period: str
    gross_usd: float
    net_usd: float
    gas_usd: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    avg_profit_usd: float
    best_usd: float
    worst_usd: float
    roi_pct: float
    sharpe: float
    max_drawdown_usd: float

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON/WS."""
        return {
            "period": self.period,
            "gross_usd": round(self.gross_usd, 4),
            "net_usd": round(self.net_usd, 4),
            "gas_usd": round(self.gas_usd, 4),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "avg_profit_usd": round(self.avg_profit_usd, 4),
            "best_usd": round(self.best_usd, 2),
            "worst_usd": round(self.worst_usd, 2),
            "roi_pct": round(self.roi_pct, 4),
            "sharpe": round(self.sharpe, 3),
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
        }


PERIODS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


class PnlTracker:
    """Аналитика по сделкам: сводки, кривая PnL, топ-разрезы."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    async def summary(self, period: str = "24h") -> PnlSummary:
        """Считает сводку PnL за период (open/close сделки учитываются по факту успеха)."""
        rows = await self._trades(period)
        gross = sum((float(t.actual_profit_usd) + float(t.gas_price_gwei) * 0) for t in rows)
        gas = sum(float(t.gas_used) * float(t.gas_price_gwei) * 1e-9 * 2650 for t in rows)
        net = sum(float(t.actual_profit_usd) for t in rows if t.status == TradeStatus.success)
        wins = [float(t.actual_profit_usd) for t in rows if t.status == TradeStatus.success
                and float(t.actual_profit_usd) > 0]
        losses = [float(t.actual_profit_usd) for t in rows if float(t.actual_profit_usd) < 0]
        payoffs = wins + losses
        mean = sum(payoffs) / len(payoffs) if payoffs else 0.0
        variance = (
            sum((p - mean) ** 2 for p in payoffs) / (len(payoffs) - 1) if len(payoffs) > 1 else 0.0
        )
        std = math.sqrt(variance)
        sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0.0
        # просадка по кумулятивной кривой
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in rows:
            cumulative += float(t.actual_profit_usd)
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)

        return PnlSummary(
            period=period,
            gross_usd=gross,
            net_usd=net,
            gas_usd=gas,
            trades=len(rows),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=(len(wins) / len(payoffs) * 100) if payoffs else 0.0,
            avg_profit_usd=mean,
            best_usd=max(wins) if wins else 0.0,
            worst_usd=min(losses) if losses else 0.0,
            roi_pct=(net / 100_000 * 100) if net else 0.0,
            sharpe=sharpe,
            max_drawdown_usd=max_dd,
        )

    async def _trades(self, period: str) -> list[Trade]:
        """Выборка сделок за период (завершённые, без pending)."""
        stmt = select(Trade).where(Trade.status != TradeStatus.pending)
        delta = PERIODS.get(period)
        if delta is not None:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - delta
            stmt = stmt.where(Trade.ts >= cutoff)
        stmt = stmt.order_by(Trade.ts.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    async def equity_curve(self, period: str = "7d", points: int = 96) -> list[dict[str, Any]]:
        """Кривая капитала: кумулятивный PnL по временным точкам (для графика дашборда)."""
        rows = await self._trades(period)
        if not rows:
            base = datetime.now(UTC).replace(tzinfo=None)
            delta = PERIODS.get(period) or timedelta(days=30)
            step = delta / points
            return [
                {"ts": (base - delta + step * i).isoformat(), "pnl_usd": 0.0, "cumulative_usd": 0.0}
                for i in range(points + 1)
            ]

        start = rows[0].ts
        end = rows[-1].ts
        span = max((end - start).total_seconds(), 1.0)
        step = span / points
        buckets = [0.0] * (points + 1)
        for t in rows:
            idx = min(points, int((t.ts - start).total_seconds() / step))
            buckets[idx] += float(t.actual_profit_usd)
        curve: list[dict[str, Any]] = []
        cumulative = 0.0
        for i, value in enumerate(buckets):
            cumulative += value
            curve.append(
                {
                    "ts": (start + timedelta(seconds=step * i)).isoformat(),
                    "pnl_usd": round(value, 4),
                    "cumulative_usd": round(cumulative, 4),
                }
            )
        return curve

    # ------------------------------------------------------------------ #
    async def top_by(self, field: str, period: str = "7d", limit: int = 10) -> list[dict[str, Any]]:
        """Топ-разрез по полю (strategy | relay | chain_id): прибыль, сделки, win rate."""
        rows = await self._trades(period)
        grouped: dict[str, dict[str, float]] = {}
        for t in rows:
            key = str(getattr(t, field, "unknown") or "unknown")
            bucket = grouped.setdefault(key, {"net": 0.0, "trades": 0.0, "wins": 0.0})
            bucket["net"] += float(t.actual_profit_usd)
            bucket["trades"] += 1
            bucket["wins"] += 1 if t.status == TradeStatus.success else 0
        out = [
            {
                "key": key,
                "net_usd": round(v["net"], 4),
                "trades": int(v["trades"]),
                "win_rate_pct": round(v["wins"] / v["trades"] * 100, 2) if v["trades"] else 0.0,
            }
            for key, v in grouped.items()
        ]
        out.sort(key=lambda item: item["net_usd"], reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ #
    async def rollup_daily(self, day: str | None = None) -> list[DailyStat]:
        """Пересобирает агрегаты ``daily_stats`` за день (идемпотентно, upsert)."""
        day = day or datetime.now(UTC).strftime("%Y-%m-%d")
        start = datetime.fromisoformat(day)
        rows = await self.session.execute(
            select(Trade).where(Trade.ts >= start, Trade.ts < start + timedelta(days=1))
        )
        grouped: dict[tuple[int, str], dict[str, Decimal | int]] = {}
        for trade in rows.scalars().all():
            key = (trade.chain_id, trade.strategy or "unknown")
            bucket = grouped.setdefault(key, {"gross": Decimal("0"), "net": Decimal("0"),
                                              "trades": 0, "wins": 0, "losses": 0, "gas": Decimal("0")})
            bucket["gross"] = Decimal(str(bucket["gross"])) + trade.actual_profit_usd
            bucket["net"] = Decimal(str(bucket["net"])) + trade.actual_profit_usd
            bucket["trades"] = int(bucket["trades"]) + 1
            if trade.status == TradeStatus.success:
                bucket["wins"] = int(bucket["wins"]) + 1
            else:
                bucket["losses"] = int(bucket["losses"]) + 1

        stats: list[DailyStat] = []
        for (chain_id, strategy), bucket in grouped.items():
            existing = await self.session.execute(
                select(DailyStat).where(
                    DailyStat.date == day,
                    DailyStat.chain_id == chain_id,
                    DailyStat.strategy == strategy,
                )
            )
            stat = existing.scalar_one_or_none() or DailyStat(
                date=day, chain_id=chain_id, strategy=strategy
            )
            stat.gross_usd = Decimal(str(bucket["gross"]))
            stat.net_usd = Decimal(str(bucket["net"]))
            stat.trades = int(bucket["trades"])
            stat.wins = int(bucket["wins"])
            stat.losses = int(bucket["losses"])
            stat.gas_usd = Decimal(str(bucket["gas"]))
            self.session.add(stat)
            stats.append(stat)
        await self.session.flush()
        return stats

    # ------------------------------------------------------------------ #
    async def totals(self) -> dict[str, float]:
        """Быстрые тоталы для дашборда (без разбивки по периодам)."""
        result = await self.session.execute(
            select(
                func.count(Trade.id),
                func.coalesce(func.sum(Trade.actual_profit_usd), 0),
            ).where(Trade.status == TradeStatus.success)
        )
        count, total = result.one()
        return {"trades": int(count or 0), "net_usd": float(total or 0)}
