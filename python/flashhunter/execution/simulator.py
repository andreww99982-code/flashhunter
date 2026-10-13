"""Локальный симулятор кандидатов.

В production основной путь — Rust ``sim-engine`` (revm, форк-состояние, параллельная
симуляция 500+ кандидатов). Этот модуль повторяет его семантику на Python:
пересчитывает выход по актуальному снимку пулов, проверяет допуск по слиппеджу
и классифицирует revert-причины так же, как это делает EVM-контракт.

Зачем дублировать: (1) демо/CI без Rust-бинарников, (2) страховочная проверка
поверх быстрой симуляции — «второй взгляд» перед отправкой.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from flashhunter.market.snapshot import MarketSnapshot
from flashhunter.strategies.base import Opportunity
from flashhunter.strategies.math.adapters import amount_out

# Причины revert, совпадающие с ошибками контрактов FlashLoanArbitrage.sol
REVERT_INSUFFICIENT_OUTPUT = "InsufficientOutputAmount"
REVERT_UNPROFITABLE = "Unprofitable()"
REVERT_STALE_PRICE = "StalePrice()"
REVERT_POOL_NOT_FOUND = "PoolNotFound()"
REVERT_HF_RECOVERED = "HealthFactorRecovered()"
REVERT_BRIDGE_FEE = "BridgeFeeTooHigh()"


@dataclass(slots=True)
class SimulationResult:
    """Результат симуляции кандидата."""

    success: bool
    output: int
    gas_used: int
    profit_usd: Decimal
    latency_us: int
    revert_reason: str = ""
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для БД/WS."""
        return {
            "success": self.success,
            "output": self.output,
            "gas_used": self.gas_used,
            "profit_usd": float(self.profit_usd),
            "latency_us": self.latency_us,
            "revert_reason": self.revert_reason,
            "checks": self.checks,
        }


class LocalSimulator:
    """Симуляция кандидата по снимку состояния (без EVM)."""

    def __init__(self, slippage_tolerance_bps: int = 30) -> None:
        self.slippage_tolerance_bps = slippage_tolerance_bps

    async def simulate(self, opp: Opportunity, snapshot: MarketSnapshot) -> SimulationResult:
        """Полный прогон кандидата: пересчёт выхода, проверка допусков, классификация ошибок."""
        started = time.perf_counter()
        try:
            if opp.kind == "cycle":
                result = self._simulate_cycle(opp, snapshot)
            elif opp.kind == "liquidation":
                result = self._simulate_liquidation(opp, snapshot)
            elif opp.kind == "backrun":
                result = self._simulate_cycle(opp, snapshot)
            elif opp.kind == "jit":
                result = self._simulate_jit(opp, snapshot)
            else:  # intent
                result = self._simulate_intent(opp, snapshot)
        except KeyError as exc:  # пул исчез из снимка
            result = SimulationResult(False, 0, 0, Decimal("0"), 0, REVERT_POOL_NOT_FOUND,
                                      {"missing": str(exc)})
        result.latency_us = int((time.perf_counter() - started) * 1e6)
        return result

    # ------------------------------------------------------------------ #
    def _simulate_cycle(self, opp: Opportunity, snapshot: MarketSnapshot) -> SimulationResult:
        """Пересчёт многохопового цикла по актуальным состояниям пулов."""
        amount = opp.size_in
        route: list[dict[str, Any]] = []
        for idx, pool_address in enumerate(opp.pools):
            pool = snapshot.pools[pool_address]
            token_in = opp.tokens[0] if idx == 0 else opp.tokens[idx]
            out = amount_out(pool, token_in, amount)
            route.append({"pool": pool.dex, "in": amount, "out": out, "kind": pool.kind})
            if out <= 0:
                return SimulationResult(False, 0, self._gas_for(opp), Decimal("0"), 0,
                                        REVERT_INSUFFICIENT_OUTPUT, {"route": route})
            amount = out

        min_out = int(opp.expected_out * (10_000 - self.slippage_tolerance_bps) / 10_000)
        if amount < min_out:
            return SimulationResult(
                False, amount, self._gas_for(opp), Decimal("0"), 0, REVERT_INSUFFICIENT_OUTPUT,
                {"actual": amount, "min_out": min_out, "route": route},
            )
        profit_units = amount - opp.size_in
        if profit_units <= 0:
            return SimulationResult(False, amount, self._gas_for(opp), Decimal("0"), 0,
                                    REVERT_UNPROFITABLE, {"route": route})
        profit_usd = snapshot.to_usd(opp.token_in, profit_units)
        return SimulationResult(
            True, amount, self._gas_for(opp), profit_usd, 0, "",
            {"route": route, "min_out": min_out, "slippage_bps": self.slippage_tolerance_bps},
        )

    def _simulate_liquidation(self, opp: Opportunity, snapshot: MarketSnapshot) -> SimulationResult:
        """Проверка ликвидации: HF всё ещё ниже 1, бонус сохраняется."""
        for position in snapshot.positions:
            if position.user == opp.meta.get("user"):
                if position.health_factor >= 1.0:
                    return SimulationResult(False, 0, self._gas_for(opp), Decimal("0"), 0,
                                            REVERT_HF_RECOVERED,
                                            {"health_factor": position.health_factor})
                return SimulationResult(True, opp.expected_out, self._gas_for(opp), opp.profit_usd,
                                        0, "", {"health_factor": position.health_factor})
        return SimulationResult(False, 0, self._gas_for(opp), Decimal("0"), 0, REVERT_STALE_PRICE)

    def _simulate_jit(self, opp: Opportunity, snapshot: MarketSnapshot) -> SimulationResult:
        """JIT: пересчёт доли комиссии по фактическому размеру свопа в блоке."""
        target = None
        for swap in snapshot.pending_swaps:
            if swap.tx_hash == opp.meta.get("swap_tx"):
                target = swap
        if target is None:
            return SimulationResult(False, 0, self._gas_for(opp), Decimal("0"), 0, REVERT_STALE_PRICE)
        pool = snapshot.pools[opp.pools[0]]
        our_liquidity = float(opp.meta.get("our_liquidity", 0))
        share = our_liquidity / (pool.liquidity + our_liquidity) if pool.liquidity > 0 else 0.0
        fee_usd = target.size_usd * Decimal(pool.fee_bps) / Decimal(10_000) * Decimal(str(share))
        if fee_usd <= 0:
            return SimulationResult(False, 0, self._gas_for(opp), Decimal("0"), 0, REVERT_UNPROFITABLE)
        return SimulationResult(True, opp.expected_out, self._gas_for(opp), fee_usd, 0, "",
                                {"fee_share_usd": float(fee_usd)})

    def _simulate_intent(self, opp: Opportunity, snapshot: MarketSnapshot) -> SimulationResult:
        """Интент: проверка, что спред не схлопнулся и комиссия бриджа в допуске."""
        local = snapshot.price(opp.token_in)
        if local <= 0:
            return SimulationResult(False, 0, 0, Decimal("0"), 0, REVERT_STALE_PRICE)
        dst = Decimal(str(opp.meta.get("dst_price_usd", "0")))
        if dst <= local:
            return SimulationResult(False, 0, 0, Decimal("0"), 0, REVERT_UNPROFITABLE,
                                    {"local": str(local), "dst": str(dst)})
        fee_bps = int(opp.meta.get("fee_bps", 0))
        if fee_bps > 40:
            return SimulationResult(False, 0, 0, Decimal("0"), 0, REVERT_BRIDGE_FEE, {"fee_bps": fee_bps})
        return SimulationResult(True, opp.expected_out, 0, opp.profit_usd, 0, "",
                                {"spread_bps": float((dst - local) / local * 10_000)})

    # ------------------------------------------------------------------ #
    @staticmethod
    def _gas_for(opp: Opportunity) -> int:
        """Оценка потребления газа по типу возможности (совпадает с контрактом)."""
        base = {
            "cycle": 340_000,
            "liquidation": 430_000,
            "jit": 250_000,
            "backrun": 300_000,
            "intent": 0,
        }.get(opp.kind, 300_000)
        return base + 45_000 * max(0, len(opp.pools) - 2)  # каждый дополнительный хоп
