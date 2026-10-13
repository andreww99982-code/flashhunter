"""Движок бэктеста FlashHunter.

Два источника данных:
  1. single-node (по умолчанию) — MockMarketSource: детерминированный генератор
     рынка с профилями calm/normal/volatile/stress. Нужен для проверки гипотез
     и подбора параметров без исторических данных.
  2. cluster — таблицы ClickHouse (opportunities/trades), когда развёрнут
     ClickHouse-кластер (ТЗ §4). Режим используется в ночных прогонах.

Экономика прогона:
  * для каждого блока считаем возможности всех выбранных стратегий;
  * берём НЕ более max_executions_per_block лучших по net-прибыли (как в live);
  * к net применяем fill_ratio — долю случаев, когда сделка реально попадает в блок
    (реалистичность: конкуренция ботов, отмены, опоздание по латентности);
  * строим кривую капитала по блокам (conservative: best-net-per-block).
"""

from __future__ import annotations

import csv
import hashlib
import logging
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from flashhunter.market.mock import MockMarketSource
from flashhunter.market.snapshot import MarketSnapshot
from flashhunter.strategies.base import BlockCtx, BaseStrategy, Opportunity
from flashhunter.strategies.math.adapters import register_decimals
from flashhunter.strategies.service import STRATEGY_CLASSES

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StrategyReport:
    """Итог прогона одной стратегии."""

    name: str
    found: int = 0
    executed: int = 0
    net_usd: float = 0.0
    gross_usd: float = 0.0
    gas_usd: float = 0.0
    best_usd: float = 0.0
    worst_usd: float = 0.0
    avg_size_usd: float = 0.0
    size_usd_total: float = 0.0
    roi_pct: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для API/CSV."""
        return {
            "strategy": self.name,
            "found": self.found,
            "executed": self.executed,
            "net_usd": round(self.net_usd, 4),
            "gross_usd": round(self.gross_usd, 4),
            "gas_usd": round(self.gas_usd, 4),
            "best_usd": round(self.best_usd, 4),
            "worst_usd": round(self.worst_usd, 4),
            "avg_size_usd": round(self.avg_size_usd, 2),
            "roi_pct": round(self.roi_pct, 4),
        }


@dataclass(slots=True)
class BacktestReport:
    """Полный отчёт прогона: параметры, итоги, разбивка по стратегиям и кривая капитала."""

    blocks: int
    seed: int
    profile: str
    duration_sec: float
    strategies: list[StrategyReport] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    opportunities_per_block: list[int] = field(default_factory=list)
    note: str = ""

    @property
    def total_found(self) -> int:
        return sum(item.found for item in self.strategies)

    @property
    def total_net_usd(self) -> float:
        return sum(item.net_usd for item in self.strategies)

    @property
    def blocks_per_sec(self) -> float:
        return self.blocks / self.duration_sec if self.duration_sec > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Формат ответа POST /api/v1/backtest/run."""
        return {
            "blocks": self.blocks,
            "seed": self.seed,
            "profile": self.profile,
            "duration_sec": round(self.duration_sec, 3),
            "blocks_per_sec": round(self.blocks_per_sec, 1),
            "total_found": self.total_found,
            "total_net_usd": round(self.total_net_usd, 2),
            "avg_per_block_usd": round(self.total_net_usd / max(1, self.blocks), 4),
            "by_strategy": [item.as_dict() for item in self.strategies],
            "equity_curve": [round(value, 4) for value in self.equity_curve],
            "opportunities_per_block": self.opportunities_per_block,
            "note": self.note,
        }

    def to_csv(self, path: str | Path) -> Path:
        """Выгружает сделки прогона в CSV (для внешнего анализа в Excel/Pandas)."""
        target = Path(path)
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(
                ["strategy", "found", "executed", "net_usd", "gross_usd", "gas_usd", "best_usd", "worst_usd", "roi_pct"]
            )
            for item in self.strategies:
                writer.writerow(
                    [
                        item.name,
                        item.found,
                        item.executed,
                        round(item.net_usd, 4),
                        round(item.gross_usd, 4),
                        round(item.gas_usd, 4),
                        round(item.best_usd, 4),
                        round(item.worst_usd, 4),
                        round(item.roi_pct, 4),
                    ]
                )
        return target


class BacktestEngine:
    """Прогон стратегий по блокам с реалистичными ограничениями live-режима."""

    def __init__(
        self,
        *,
        strategies: Iterable[str] | None = None,
        profile: str = "normal",
        seed: int = 42,
        min_profit_usd: float = 10.0,
        max_executions_per_block: int = 2,
        fill_ratio: float = 0.85,
        slippage_factor: float = 1.0,
        params_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.profile = profile
        self.seed = seed
        self.min_profit_usd = Decimal(str(min_profit_usd))
        self.max_executions_per_block = max_executions_per_block
        # как часто сделка реально попадает в блок (учитываем конкуренцию MEV-ботов)
        self.fill_ratio = fill_ratio
        # множитель к прибыли: 1.0 — идеальные условия, 0.7 — «пессимистичный» сценарий
        self.slippage_factor = slippage_factor
        self.params_overrides = params_overrides or {}

        names = list(strategies) if strategies else ["dex_arb", "triangular", "stable_peg"]
        self.strategy_names = names
        self.strategies: list[BaseStrategy] = []
        for name in names:
            cls = STRATEGY_CLASSES.get(name)
            if cls is None:
                raise ValueError(f"неизвестная стратегия: {name}")
            params = self.params_overrides.get(name)
            self.strategies.append(cls(params=params) if params else cls())

    # ------------------------------------------------------------------ #
    def run_mock(self, blocks: int = 100) -> BacktestReport:
        """Прогон на детерминированном мок-рынке (single-node режим)."""
        return self._run(blocks, MockMarketSource(seed=self.seed, profile=self.profile))

    def run_replay(self, snapshots: Iterable[MarketSnapshot], blocks: int | None = None) -> BacktestReport:
        """Прогон на сохранённых снимках (реплей архива кластера).

        Каждый снимок — это состояние одного блока: используется для валидации
        параметров на исторических данных без живого RPC.
        """
        return self._run(blocks or 0, iter(snapshots))

    # ------------------------------------------------------------------ #
    def _run(self, blocks: int, source: Iterable[MarketSnapshot] | MockMarketSource) -> BacktestReport:
        """Общий цикл прогона: снимок → возможности → отбор лучших → отчёт."""
        started = time.perf_counter()
        reports: dict[str, StrategyReport] = {name: StrategyReport(name=name) for name in self.strategy_names}
        equity: list[float] = []
        per_block_counts: list[int] = []
        cumulative = 0.0
        processed = 0
        iterator = self._iterate(source, blocks)

        for snapshot in iterator:
            processed += 1
            ctx = BlockCtx(
                chain_id=snapshot.chain_id,
                block_number=snapshot.block_number,
                timestamp=snapshot.timestamp,
                base_fee_gwei=snapshot.base_fee_gwei,
                snapshot=snapshot,
            )
            register_decimals(snapshot)

            found: list[Opportunity] = []
            for strategy in self.strategies:
                if not strategy.supports(ctx.chain_id):
                    continue
                opportunities = strategy.on_block_sync(ctx) if hasattr(strategy, "on_block_sync") else []
                if not opportunities:
                    continue
                report = reports[strategy.name]
                report.found += len(opportunities)
                for opportunity in opportunities:
                    # gross = net + издержки (газ и комиссия флеш-кредита)
                    report.gross_usd += float(
                        opportunity.net_profit_usd + opportunity.gas_usd + opportunity.flash_fee_usd
                    )
                    report.gas_usd += float(opportunity.gas_usd)
                    found.append(opportunity)

            # отбор: как в live — не больше max_executions_per_block лучших по net
            found.sort(key=lambda item: item.net_profit_usd, reverse=True)
            selected = found[: self.max_executions_per_block]
            block_profit = 0.0

            for opportunity in selected:
                if opportunity.net_profit_usd < self.min_profit_usd:
                    continue
                # часть сделок «не долетает» до блока (конкуренция, отмены, латентность)
                executed = self._fills(processed, opportunity)
                report = reports[opportunity.strategy]
                if not executed:
                    continue
                net = float(opportunity.net_profit_usd) * self.slippage_factor
                report.executed += 1
                report.net_usd += net
                report.best_usd = max(report.best_usd, net)
                report.worst_usd = min(report.worst_usd, net) if report.executed > 1 else net
                report.size_usd_total += float(opportunity.size_usd)
                block_profit += net

            cumulative += block_profit
            equity.append(cumulative)
            per_block_counts.append(len(found))

        # финализация метрик
        for report in reports.values():
            if report.executed:
                report.avg_size_usd = report.size_usd_total / report.executed
                # ROI считается по суммарному обороту капитала, а не по средней сделке
                report.roi_pct = report.net_usd / report.size_usd_total * 100 if report.size_usd_total else 0.0

        duration = time.perf_counter() - started
        note = (
            "Данные: MockMarketSource (single-node). В cluster-режиме источник — ClickHouse "
            "(таблицы opportunities/trades). fill_ratio учитывает конкуренцию MEV-ботов."
        )
        return BacktestReport(
            blocks=processed,
            seed=self.seed,
            profile=self.profile,
            duration_sec=duration,
            strategies=[reports[name] for name in self.strategy_names],
            equity_curve=equity,
            opportunities_per_block=per_block_counts,
            note=note,
        )

    # ------------------------------------------------------------------ #
    def _iterate(self, source: Iterable[MarketSnapshot] | MockMarketSource, blocks: int) -> Iterable[MarketSnapshot]:
        """Итератор снимков: мок-источник генерирует, реплей — отдаёт заранее снятые."""
        if isinstance(source, MockMarketSource):
            for _ in range(max(1, blocks)):
                yield source.next_snapshot()
        else:
            yield from source

    def _fills(self, block_index: int, opportunity: Opportunity) -> bool:
        """Детерминированный «псевдослучайный» отбор сделок, долетевших до блока.

        Хэш sha256(block, uid, seed) → [0,1). Встроенный hash() здесь не годится:
        для строк он солится PYTHONHASHSEED и меняется между запусками процесса,
        а отчёт бэктеста должен воспроизводиться побайтно.
        """
        # uid генерируется случайно для каждого объекта, поэтому берём УСТОЙЧИВЫЕ поля:
        # блок + стратегия + токены + пулы. Одинаковый рынок → одинаковый отбор сделок.
        signature = "|".join(
            [
                str(block_index),
                opportunity.strategy,
                opportunity.token_in,
                opportunity.token_out,
                ",".join(opportunity.pools),
                str(self.seed),
            ]
        )
        digest = hashlib.sha256(signature.encode()).digest()
        fraction = int.from_bytes(digest[:4], "big") / 2**32
        return fraction < self.fill_ratio

    # ------------------------------------------------------------------ #
    def grid_search(
        self,
        grid: dict[str, list[Any]],
        blocks: int = 150,
        metric: Callable[[BacktestReport], float] | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        """Перебор параметров: возвращает список (параметры, метрика) по убыванию метрики.

        Пример: grid_search({"min_profit_usd": [5, 10, 25], "fill_ratio": [0.7, 0.85, 1.0]}).
        """
        score = metric or (lambda report: report.total_net_usd)
        results: list[tuple[dict[str, Any], float]] = []
        for combination in _product(grid):
            original = {key: getattr(self, key) for key in combination}
            for key, value in combination.items():
                setattr(self, key, value)
            try:
                report = self.run_mock(blocks=blocks)
                results.append((dict(combination), score(report)))
            finally:
                for key, value in original.items():
                    setattr(self, key, value)
        results.sort(key=lambda item: item[1], reverse=True)
        return results


def _product(grid: dict[str, list[Any]]) -> Iterable[dict[str, Any]]:
    """Декартово произведение значений параметров (без зависимости от itertools.product)."""
    keys = list(grid)
    if not keys:
        return
    indices = [0] * len(keys)
    while True:
        yield {key: grid[key][indices[i]] for i, key in enumerate(keys)}
        position = len(keys) - 1
        while position >= 0:
            indices[position] += 1
            if indices[position] < len(grid[keys[position]]):
                break
            indices[position] = 0
            position -= 1
        if position < 0:
            return


def summarize_curve(equity: list[float]) -> dict[str, float]:
    """Метрики качества кривой капитала (используется в отчётах и тестах)."""
    if not equity:
        return {"final": 0.0, "max_drawdown": 0.0, "volatility": 0.0, "sharpe": 0.0}
    peak = equity[0]
    max_drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    returns = [equity[i] - equity[i - 1] for i in range(1, len(equity))]
    volatility = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    mean_return = statistics.fmean(returns) if returns else 0.0
    sharpe = mean_return / volatility * (len(returns) ** 0.5) if volatility > 0 else 0.0
    return {
        "final": round(equity[-1], 4),
        "max_drawdown": round(max_drawdown, 4),
        "volatility": round(volatility, 4),
        "sharpe": round(sharpe, 3),
    }


__all__ = ["BacktestEngine", "BacktestReport", "StrategyReport", "summarize_curve"]
