"""Stable-peg арбитраж: отклонения стейблкоинов от паритета (Curve/Convex, V3 stable pairs).

Отличие от обычного DEX-арбитража: спреды измеряются в единицах bps (2–50 bps),
окно живёт доли секунды, а газ съедает значимую часть прибыли. Поэтому стратегия
жёстко фильтрует по базовой комиссии блока (``max_base_fee_gwei``) и требует
минимальный ROI, компенсирующий риск незаполнения.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from itertools import combinations

from flashhunter.market.snapshot import PoolSnapshot
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, gas_cost_usd
from flashhunter.strategies.math.adapters import amount_out, probe_amount, spot_rate
from flashhunter.strategies.math.amm import ternary_search_optimal_size

log = logging.getLogger(__name__)

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDE", "FRAX", "LUSD", "TUSD", "USDS"}
PEG_TOLERANCE_BPS = 500  # 5% — за пределами уже не «peg-арбитраж», а событие депега


class StablePegStrategy(BaseStrategy):
    """Ловит микросдвиги паритета стейблов между stableswap/V2/V3 пулами."""

    name = "stable_peg"  # type: ignore[assignment]
    chains = [1, 137, 42161]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.min_deviation_bps = float(self.param("min_deviation_bps", 1.0))
        self.max_deviation_bps = float(self.param("max_deviation_bps", 50.0))
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 8.0)))
        self.min_size_usd = Decimal(str(self.param("min_size_usd", 2_000.0)))
        self.max_size_usd = Decimal(str(self.param("max_size_usd", 400_000.0)))
        self.max_base_fee_gwei = float(self.param("max_base_fee_gwei", 18.0))
        self.gas_units = int(self.param("gas_units", 260_000))
        self.top_n = int(self.param("top_n", 6))

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Ищет расхождения паритета между всеми пулами стейбл-пар в блоке."""
        if ctx.base_fee_gwei > self.max_base_fee_gwei:
            log.debug("stable_peg: base fee %.2f gwei > ceiling — пропуск блока", ctx.base_fee_gwei)
            return []

        snapshot = ctx.snapshot
        grouped: dict[tuple[str, str], list[PoolSnapshot]] = {}
        for pool in snapshot.pools.values():
            s0 = snapshot.token(pool.token0).symbol
            s1 = snapshot.token(pool.token1).symbol
            if s0 in STABLE_SYMBOLS and s1 in STABLE_SYMBOLS and pool.chain_id == ctx.chain_id:
                key = tuple(sorted((pool.token0, pool.token1)))  # type: ignore[arg-type]
                grouped.setdefault(key, []).append(pool)

        found: list[Opportunity] = []
        for (token_a, token_b), pools in grouped.items():
            for pool_a, pool_b in combinations(pools, 2):
                deviation_bps = self._deviation_bps(pool_a, pool_b, snapshot, token_a)
                if not (self.min_deviation_bps <= deviation_bps <= self.max_deviation_bps):
                    continue
                opp = self._evaluate(ctx, pool_a, pool_b, token_a, token_b, deviation_bps)
                if opp is not None:
                    found.append(opp)
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return found[: self.top_n]

    # ------------------------------------------------------------------ #
    def _deviation_bps(self, pool_a: PoolSnapshot, pool_b: PoolSnapshot, snapshot, token_in: str) -> float:
        """Расхождение курсов двух пулов в bps (без учёта комиссий)."""
        rate_a = spot_rate(pool_a, snapshot, token_in)
        rate_b = spot_rate(pool_b, snapshot, token_in)
        if rate_a <= 0 or rate_b <= 0:
            return 0.0
        return float(abs(rate_a - rate_b) / max(rate_a, rate_b) * Decimal(10_000))

    def _evaluate(
        self,
        ctx: BlockCtx,
        pool_a: PoolSnapshot,
        pool_b: PoolSnapshot,
        token_a: str,
        token_b: str,
        deviation_bps: float,
    ) -> Opportunity | None:
        """Оценивает цикл token_a -> token_b (pool_a) -> token_a (pool_b)."""
        snapshot = ctx.snapshot
        # направление: покупаем там, где токен дешевле
        rate_a = spot_rate(pool_a, snapshot, token_a)
        rate_b = spot_rate(pool_b, snapshot, token_a)
        buy_pool, sell_pool = (pool_a, pool_b) if rate_a > rate_b else (pool_b, pool_a)

        min_size = probe_amount(buy_pool, snapshot, token_a, self.min_size_usd)
        max_size = probe_amount(buy_pool, snapshot, token_a, self.max_size_usd)
        if min_size <= 0 or max_size <= min_size:
            return None

        def profit_fn(dx: int) -> float:
            mid = amount_out(buy_pool, token_a, dx)
            if mid <= 0:
                return float(-dx)
            return float(amount_out(sell_pool, token_b, mid) - dx)

        size = ternary_search_optimal_size(profit_fn, min_size, max_size, iterations=40)
        out_amount = amount_out(sell_pool, token_b, amount_out(buy_pool, token_a, size))
        gross = out_amount - size
        if gross <= 0:
            return None

        size_usd = snapshot.to_usd(token_a, size)
        profit_usd = snapshot.to_usd(token_a, gross)
        gas_usd = gas_cost_usd(
            gas_units=self.gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=float(self.param("priority_fee_gwei", 1.5)),
            native_price_usd=snapshot.native_price_usd,
        )
        flash_fee_usd = size_usd * Decimal(5) / Decimal(10_000)
        net = profit_usd - gas_usd - flash_fee_usd
        if net < self.min_profit_usd:
            return None

        roi = float(net / size_usd * 100) if size_usd > 0 else 0.0
        # стабильный арб чувствителен к гонке за включение: уверенность ниже, чем у широких спредов
        confidence = min(0.85, 0.35 + deviation_bps / 100 * 0.5 + min(roi, 0.5) * 0.5)

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=token_a,
            token_out=token_b,
            size_in=size,
            expected_out=out_amount,
            profit_value=Decimal(gross),
            profit_usd=profit_usd,
            gas_usd=gas_usd,
            flash_fee_usd=flash_fee_usd,
            confidence=confidence,
            block_number=ctx.block_number,
            dex_buy=buy_pool.dex,
            dex_sell=sell_pool.dex,
            pools=[buy_pool.address, sell_pool.address],
            tokens=[token_a, token_b],
            kind="cycle",
            meta={
                "size_usd": str(size_usd),
                "deviation_bps": round(deviation_bps, 3),
                "symbol_in": snapshot.token(token_a).symbol,
                "symbol_out": snapshot.token(token_b).symbol,
                "stable_peg": True,
                "roi_pct": roi,
            },
        )
