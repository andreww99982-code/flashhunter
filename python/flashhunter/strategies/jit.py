"""JIT (Just-In-Time) ликвидность в Uniswap V3/V4.

Идея: увидев в мемпуле крупный своп на пуле с узким диапазоном,
мы добавляем ликвидность в диапазон прямо перед свопом и снимаем сразу после,
забирая почти всю комиссию свопа. Риск минимален (позиция живёт один блок),
главная издержка — газ на mint+burn (~250k) и риск не попасть в тот же блок.

Прибыль = доля комиссии свопа, пропорциональная нашей ликвидности в АКТИВНОМ
диапазоне (а не во всём пуле — в этом вся суть JIT):
    fee_share = swap_size * fee_bps/10000 * capital / (capital + active_range_depth)
    net       = fee_share - gas(mint+burn) - residual_inventory_risk
"""

from __future__ import annotations

import logging
from decimal import Decimal

from flashhunter.market.snapshot import MarketSnapshot, PendingSwap
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, TxCtx, gas_cost_usd

log = logging.getLogger(__name__)


class JitStrategy(BaseStrategy):
    """JIT-ликвидность под крупные свопы из мемпула."""

    name = "jit"  # type: ignore[assignment]
    chains = [1, 8453]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.min_swap_usd = Decimal(str(self.param("min_swap_usd", 50_000.0)))
        self.range_width_bps = int(self.param("range_width_bps", 10))  # ±0.05% вокруг цены
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 5.0)))
        # капитал, который мы готовы выставить в диапазон на один блок
        self.jit_capital_usd = Decimal(str(self.param("jit_capital_usd", 750_000.0)))
        # доля TVL пула, сидящая в АКТИВНОМ диапазоне: у V3 она мала (~0.5%) — в этом и смысл JIT
        self.active_depth_pct = float(self.param("active_depth_pct", 0.005))
        # остаточный риск несовпадения инвентаря (JIT снимается тем же блоком)
        self.il_bps = float(self.param("il_bps", 0.5))
        self.max_il_usd = Decimal(str(self.param("max_il_usd", 500.0)))
        self.gas_units = int(self.param("gas_units", 250_000))  # mint + burn
        self.max_jit_size_usd = Decimal(str(self.param("max_jit_size_usd", 5_000_000.0)))
        self.top_n = int(self.param("top_n", 3))

    # ------------------------------------------------------------------ #
    async def on_tx(self, ctx: TxCtx) -> list[Opportunity]:
        """Реакция на крупный своп в мемпуле (реальное окно принятия решения ~10 мс)."""
        swap = ctx.pending
        if swap.size_usd < self.min_swap_usd:
            return []
        # JIT требует точного знания состояния пула на момент включения блока,
        # поэтому окончательное решение принимает on_block; здесь — предварительный сигнал.
        return []

    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Оценивает JIT-возможности по крупным свопам, доехавшим до блока."""
        snapshot = ctx.snapshot
        found: list[Opportunity] = []
        for swap in snapshot.pending_swaps:
            if swap.chain_id != snapshot.chain_id or swap.size_usd < self.min_swap_usd:
                continue
            opp = self._evaluate(ctx, swap)
            if opp is not None:
                found.append(opp)
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return found[: self.top_n]

    def _evaluate(self, ctx: BlockCtx, swap: PendingSwap) -> Opportunity | None:
        """Считает долю комиссии свопа при нашем размере JIT-позиции."""
        snapshot = ctx.snapshot
        pool = snapshot.pools.get(swap.pool_address)
        if pool is None or pool.kind != "v3" or pool.liquidity <= 0:
            return None

        # Доля комиссии считается не от всего TVL, а от АКТИВНОГО диапазона:
        # JIT-бот выставляет узкую (±range_width_bps) ликвидность и забирает
        # пропорциональную ей часть комиссии свопа.
        active_depth_usd = Decimal(str(float(pool.tvl_usd) * self.active_depth_pct))
        share = float(self.jit_capital_usd / (self.jit_capital_usd + active_depth_usd)) if active_depth_usd > 0 else 0.0
        fee_usd = swap.size_usd * Decimal(pool.fee_bps) / Decimal(10_000)
        fee_share_usd = fee_usd * Decimal(str(round(share, 6)))

        gas_usd = gas_cost_usd(
            gas_units=self.gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=float(self.param("priority_fee_gwei", 2.0)),
            native_price_usd=snapshot.native_price_usd,
        )
        # Риск: позиция живёт один блок (mint → swap → burn в одной транзакции),
        # поэтому остаётся лишь несовпадение инвентаря ~il_bps от капитала.
        position_usd = min(self.jit_capital_usd, self.max_jit_size_usd)
        il_usd = position_usd * Decimal(str(self.il_bps)) / Decimal(10_000)
        net = fee_share_usd - gas_usd - il_usd
        if net < self.min_profit_usd or il_usd > self.max_il_usd:
            return None

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=swap.token_in,
            token_out=swap.token_out,
            size_in=swap.amount_in,
            expected_out=int(swap.amount_in * 10**12 // 10**12),  # JIT не меняет размер свопа
            profit_value=fee_share_usd,
            profit_usd=fee_share_usd,
            gas_usd=gas_usd,
            flash_fee_usd=Decimal("0"),  # JIT финансируется из собственного пула
            confidence=min(0.9, 0.5 + float(fee_share_usd - gas_usd) / 500),
            block_number=ctx.block_number,
            dex_buy=swap.dex,
            dex_sell=swap.dex,
            pools=[swap.pool_address],
            tokens=[swap.token_in, swap.token_out],
            kind="jit",
            meta={
                "size_usd": str(position_usd),
                "swap_tx": swap.tx_hash,
                "swap_size_usd": str(swap.size_usd),
                "range_width_bps": self.range_width_bps,
                "our_liquidity_share": round(share, 4),
                "active_depth_usd": float(active_depth_usd),
                "fee_share_usd": float(fee_share_usd),
                "il_usd": float(il_usd),
                "roi_pct": float(net / position_usd * 100) if position_usd > 0 else 0.0,
            },
        )
