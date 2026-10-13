"""Стратегия DEX-DEX арбитража (2 хопа: X -> Y на пуле A, Y -> X на пуле B).

Математика:
    * V2↔V2 — точный оптимум в закрытой форме (``optimal_size_two_pool``);
    * прочие комбинации (V3/Curve) — тернарный поиск по размеру входа, так как
      функция прибыли вогнута и унимодальна на диапазоне до «истощения» пулов.

Учитываются: комиссии пулов, газ (по base fee и оценке газа), комиссия флеш-займа
(Aave V3 0.05% или Balancer 0), проскальзывание (неявно — через точный расчёт
выхода AMM), минимальный ROI.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from itertools import combinations

from flashhunter.market.snapshot import MarketSnapshot, PoolSnapshot
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, gas_cost_usd
from flashhunter.strategies.math.adapters import amount_out, pool_spot_price, probe_amount
from flashhunter.strategies.math.amm import (
    PoolV2,
    optimal_size_two_pool,
    ternary_search_optimal_size,
)

log = logging.getLogger(__name__)

FLASH_LOAN_FEE_BPS = {"aave_v3": 5, "balancer": 0}  # 0.05% / 0%
DEFAULT_PROVIDER = "aave_v3"


class DexArbStrategy(BaseStrategy):
    """Классический кросс-DEX арбитраж по одной паре токенов."""

    name = "dex_arb"  # type: ignore[assignment]
    chains = [1, 8453, 42161]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 12.0)))
        self.min_size_usd = Decimal(str(self.param("min_size_usd", 500.0)))
        self.max_size_usd = Decimal(str(self.param("max_size_usd", 250_000.0)))
        self.min_roi_pct = float(self.param("min_roi_pct", 0.05))
        # Верхняя граница доходности за блок. Настоящий арбитраж на ликвидных пулах
        # даёт доли процента; десятки процентов означают ошибку данных, и по такой
        # «возможности» нельзя отправлять реальные деньги — считаем её мусором.
        self.max_roi_pct = float(self.param("max_roi_pct", 25.0))
        self.rejected_anomalies = 0
        self.max_pairs = int(self.param("max_pairs", 40))
        self.gas_units = int(self.param("gas_units", 340_000))  # flash loan + 2 свопа + repayment
        self.top_n = int(self.param("top_n", 12))
        # Порог отсева пары по спотовой цене: запас, который должен покрыть комиссии
        # двух свопов, флеш-кредит и газ. 3 bps отсекает «шум», оставляя реальные идеи.
        self.screen_edge = float(self.param("screen_edge", 0.0003))
        self.last_screened = 0

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Сканирует все пары пулов снимка и возвращает лучшие арбитражи."""
        snapshot = ctx.snapshot
        pairs = self._pairs(snapshot)
        found: list[Opportunity] = []
        for token_x, token_y, pools in pairs[: self.max_pairs]:
            found.extend(self._scan_pair(ctx, token_x, token_y, pools))
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return found[: self.top_n]

    # ------------------------------------------------------------------ #
    def _pairs(self, snapshot: MarketSnapshot) -> list[tuple[str, str, list[PoolSnapshot]]]:
        """Собирает пары токенов, у которых есть ≥2 пула (потенциальный арбитраж)."""
        grouped: dict[tuple[str, str], list[PoolSnapshot]] = {}
        for pool in snapshot.pools.values():
            if pool.chain_id != snapshot.chain_id:
                continue
            key = tuple(sorted((pool.token0.lower(), pool.token1.lower())))
            grouped.setdefault(key, []).append(pool)  # type: ignore[arg-type]
        result: list[tuple[str, str, list[PoolSnapshot]]] = []
        for (a, b), pools in grouped.items():
            if len(pools) >= 2:
                result.append((a, b, pools))
        return result

    def _scan_pair(
        self, ctx: BlockCtx, token_x: str, token_y: str, pools: list[PoolSnapshot]
    ) -> list[Opportunity]:
        """Перебирает упорядоченные пары пулов (buy_on, sell_on) и оптимизирует размер."""
        out: list[Opportunity] = []
        skipped = 0
        for pool_a, pool_b in combinations(pools, 2):
            # два независимых направления одного и того же набора пулов
            for token_in, token_out in ((token_x, token_y), (token_y, token_x)):
                # Дешёвая проверка до численной оптимизации: покупаем там, где цена
                # ниже, продаём там, где выше. Если разница не покрывает комиссии
                # хотя бы с запасом — размер можно не считать (на живом рынке так
                # отсеивается подавляющее большинство пар).
                buy_price = pool_spot_price(pool_a, token_in)
                sell_price = pool_spot_price(pool_b, token_out)
                if buy_price <= 0 or sell_price <= 0 or sell_price / buy_price <= 1 + self.screen_edge:
                    skipped += 1
                    continue
                opp = self._evaluate(ctx, pool_a, pool_b, token_in, token_out)
                if opp is not None:
                    out.append(opp)
        self.last_screened = skipped
        return out

    def _evaluate(
        self,
        ctx: BlockCtx,
        buy_pool: PoolSnapshot,
        sell_pool: PoolSnapshot,
        token_in: str,
        token_out: str,
    ) -> Opportunity | None:
        """Оценивает конкретный цикл: token_in -> token_out (buy_pool) -> token_in (sell_pool)."""
        snapshot = ctx.snapshot
        min_size_units = probe_amount(buy_pool, snapshot, token_in, self.min_size_usd)
        max_size_units = probe_amount(buy_pool, snapshot, token_in, self.max_size_usd)
        if min_size_units <= 0 or max_size_units <= min_size_units:
            return None

        def profit_fn(dx: int) -> float:
            """profit(dx) = out_after_cycle(dx) - dx (в единицах token_in)."""
            mid = amount_out(buy_pool, token_in, dx)
            if mid <= 0:
                return float(-dx)
            back = amount_out(sell_pool, token_out, mid)
            return float(back - dx)

        # --- выбор метода оптимизации ---------------------------------- #
        if buy_pool.kind == "v2" and sell_pool.kind == "v2":
            from flashhunter.strategies.math.adapters import v2_pool

            size = optimal_size_two_pool(
                v2_pool(buy_pool, token_in), v2_pool(sell_pool, token_out), max_size=max_size_units
            )
            if size <= 0:
                return None
        else:
            size = ternary_search_optimal_size(profit_fn, min_size_units, max_size_units, iterations=40)
            if profit_fn(size) <= 0:
                return None

        mid_amount = amount_out(buy_pool, token_in, size)
        final_amount = amount_out(sell_pool, token_out, mid_amount)
        gross_profit = final_amount - size
        if gross_profit <= 0:
            return None

        size_usd = snapshot.to_usd(token_in, size)
        profit_usd = snapshot.to_usd(token_in, gross_profit)
        gas_usd = gas_cost_usd(
            gas_units=self.gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=float(self.param("priority_fee_gwei", 2.0)),
            native_price_usd=snapshot.native_price_usd,
        )
        flash_fee_usd = size_usd * Decimal(FLASH_LOAN_FEE_BPS[DEFAULT_PROVIDER]) / Decimal(10_000)
        net = profit_usd - gas_usd - flash_fee_usd
        if net < self.min_profit_usd:
            return None
        roi = float(net / size_usd * 100) if size_usd > 0 else 0.0
        if roi < self.min_roi_pct:
            return None

        # confidence: грубая оценка устойчивости (спред, размер, свежесть)
        in_info = snapshot.token(token_in)
        out_info = snapshot.token(token_out)
        confidence = min(0.99, 0.45 + min(roi, 1.0) * 0.35 + min(float(size_usd) / 100_000, 0.15))

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=token_in,
            token_out=token_out,
            size_in=size,
            expected_out=final_amount,
            profit_value=Decimal(gross_profit),
            profit_usd=profit_usd,
            gas_usd=gas_usd,
            flash_fee_usd=flash_fee_usd,
            confidence=confidence,
            block_number=ctx.block_number,
            dex_buy=buy_pool.dex,
            dex_sell=sell_pool.dex,
            pools=[buy_pool.address, sell_pool.address],
            tokens=[token_in, token_out],
            kind="cycle",
            meta={
                "size_usd": str(size_usd),
                "mid_amount": mid_amount,
                "symbol_in": in_info.symbol,
                "symbol_out": out_info.symbol,
                "flash_provider": DEFAULT_PROVIDER,
                "hops": 2,
                "roi_pct": roi,
            },
        )
