"""Бэктест: прогон стратегий по историческим данным (в cluster-режиме — из ClickHouse).

В single-node режиме используется генератор MockMarketSource с фиксированным seed —
это даёт воспроизводимый полигон: те же профили волатильности, что и в live-демо.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from flashhunter.api.deps import CurrentUser, service
from flashhunter.market.mock import MockMarketSource
from flashhunter.strategies.base import BlockCtx
from flashhunter.strategies.math.adapters import register_decimals
from flashhunter.strategies.service import STRATEGY_CLASSES

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    """Параметры прогона бэктеста."""

    blocks: int = Field(default=100, ge=1, le=2000)
    seed: int = Field(default=42)
    profile: str = Field(default="normal", pattern="^(calm|normal|volatile|stress)$")
    strategies: list[str] = Field(default_factory=lambda: ["dex_arb", "triangular", "stable_peg"])
    min_profit_usd: float = Field(default=10.0, ge=0)

    @field_validator("strategies")
    @classmethod
    def _known_strategies(cls, value: list[str]) -> list[str]:
        """Проверяет, что запрошены реально существующие стратегии (иначе 422, а не пустой отчёт)."""
        unknown = sorted(set(value) - set(STRATEGY_CLASSES))
        if unknown:
            raise ValueError(f"неизвестные стратегии: {', '.join(unknown)}")
        if not value:
            raise ValueError("нужно выбрать хотя бы одну стратегию")
        return value


@router.post("/run")
async def run_backtest(payload: BacktestRequest, user: CurrentUser) -> dict[str, Any]:
    """Прогоняет выбранные стратегии по N блокам и возвращает агрегированную статистику."""
    started = time.perf_counter()
    source = MockMarketSource(seed=payload.seed, profile=payload.profile)
    strategies = []
    for name in payload.strategies:
        cls = STRATEGY_CLASSES.get(name)
        if cls is None:
            continue
        strategy = cls(params={"min_profit_usd": payload.min_profit_usd})
        await strategy.on_start()
        strategies.append(strategy)

    per_strategy: dict[str, dict[str, float]] = {
        s.name: {"found": 0.0, "net_usd": 0.0, "best_usd": 0.0, "size_usd": 0.0} for s in strategies
    }
    equity: list[dict[str, Any]] = []
    cumulative = 0.0

    for _ in range(payload.blocks):
        snapshot = source.next_snapshot()
        register_decimals(snapshot)
        ctx = BlockCtx(snapshot.chain_id, snapshot.block_number, snapshot.timestamp,
                       snapshot.base_fee_gwei, snapshot)
        best_net = 0.0
        for strategy in strategies:
            found = await strategy.on_block(ctx)
            bucket = per_strategy[strategy.name]
            bucket["found"] += len(found)
            for opp in found:
                net = float(opp.net_profit_usd)
                bucket["net_usd"] += net
                bucket["best_usd"] = max(bucket["best_usd"], net)
                bucket["size_usd"] += float(opp.size_usd)
                best_net = max(best_net, net)
        cumulative += best_net  # консервативно: в блоке исполняется лучшая возможность
        equity.append({"block": snapshot.block_number, "cumulative_usd": round(cumulative, 4)})

    for strategy in strategies:
        await strategy.on_stop()

    total_found = sum(int(v["found"]) for v in per_strategy.values())
    total_net = sum(v["net_usd"] for v in per_strategy.values())
    elapsed = time.perf_counter() - started
    svc = service()
    _ = svc
    return {
        "params": payload.model_dump(),
        "blocks": payload.blocks,
        "duration_sec": round(elapsed, 3),
        "blocks_per_sec": round(payload.blocks / max(elapsed, 1e-6), 1),
        "total_found": total_found,
        "total_net_usd": round(total_net, 2),
        "avg_per_block_usd": round(total_net / payload.blocks, 4),
        "by_strategy": [
            {
                "strategy": name,
                "found": int(v["found"]),
                "net_usd": round(v["net_usd"], 2),
                "best_usd": round(v["best_usd"], 2),
                "avg_size_usd": round(v["size_usd"] / v["found"], 2) if v["found"] else 0.0,
            }
            for name, v in per_strategy.items()
        ],
        "equity_curve": equity,
        "note": "Данные: MockMarketSource (single-node режим). В cluster — ClickHouse opportunities/trades.",
    }


@router.get("/profiles")
async def profiles(user: CurrentUser) -> dict[str, Any]:
    """Доступные профили рынка для бэктеста."""
    return {
        "profiles": ["calm", "normal", "volatile", "stress"],
        "strategies": sorted(STRATEGY_CLASSES),
        "extra_params": {"min_profit_usd": float(Decimal("10"))},
    }
