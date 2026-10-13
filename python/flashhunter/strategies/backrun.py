"""Backrun: арбитраж, возникающий ПОСЛЕ крупного свопа (без сэндвича!).

Этическая политика проекта: сэндвич-атаки запрещены (см. ТЗ п.18).
Разрешённый паттерн — backrun: крупный своп сдвигает цену пула, мы
выравниваем её с остальным рынком в том же блоке, получая прибыль и
улучшая цену для последующих трейдеров.

Реализация: применяем ожидаемый своп к копии состояния пула, затем ищем
оптимальный компенсирующий арбитраж между «сдвинутым» пулом и лучшим альтернативным.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from flashhunter.market.snapshot import PoolSnapshot
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, gas_cost_usd
from flashhunter.strategies.math.adapters import amount_out, apply_swap, probe_amount
from flashhunter.strategies.math.amm import ternary_search_optimal_size

log = logging.getLogger(__name__)


class BackrunStrategy(BaseStrategy):
    """Компенсирующий арбитраж после крупных свопов."""

    name = "backrun"  # type: ignore[assignment]
    chains = [1, 8453]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.min_swap_usd = Decimal(str(self.param("min_swap_usd", 25_000.0)))
        self.max_price_impact_bps = int(self.param("max_price_impact_bps", 60))
        # ниже 3 бп компенсировать нечего: издержки съедают всю дистанцию
        self.min_impact_bps = float(self.param("min_impact_bps", 3.0))
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 10.0)))
        self.min_size_usd = Decimal(str(self.param("min_size_usd", 1_000.0)))
        self.max_size_usd = Decimal(str(self.param("max_size_usd", 300_000.0)))
        self.gas_units = int(self.param("gas_units", 300_000))
        self.top_n = int(self.param("top_n", 4))
        # защита от превращения в сэндвич: запрещаем исполнение в том же направлении,
        # что и жертва, и требуем компенсирующего (противонаправленного) свопа
        self.sandwich = False

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Для каждого крупного свопа строит компенсирующий арбитраж."""
        snapshot = ctx.snapshot
        found: list[Opportunity] = []
        for swap in snapshot.pending_swaps:
            if swap.chain_id != snapshot.chain_id or swap.size_usd < self.min_swap_usd:
                continue
            pool = snapshot.pools.get(swap.pool_address)
            if pool is None:
                continue
            opp = self._evaluate(ctx, pool, swap.token_in, swap.token_out, swap.amount_in, swap.size_usd)
            if opp is not None:
                found.append(opp)
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return found[: self.top_n]

    def _evaluate(
        self,
        ctx: BlockCtx,
        victim_pool: PoolSnapshot,
        token_in: str,
        token_out: str,
        amount_in: int,
        swap_usd: Decimal,
    ) -> Opportunity | None:
        """Моделирует последствия свопа и ищет компенсирующий цикл против остальных пулов."""
        snapshot = ctx.snapshot
        # 1. Копия пула с применённым свопом жертвы (состояние, которое увидит следующий своп)
        shifted = replace(victim_pool)
        amount_out_victim = amount_out(shifted, token_in, amount_in)
        if amount_out_victim <= 0:
            return None
        apply_swap(shifted, token_in, amount_in)

        # 2. Оценка ценового импакта жертвы: если он мал — backrun не окупается
        impact = self._impact_bps(victim_pool, shifted, token_out, token_in)
        if impact < self.min_impact_bps or impact > self.max_price_impact_bps:
            # слишком слабый сдвиг не окупает газ, слишком сильный — уже не «цена»,
            # а результат спорного допущения о размере свопа
            return None

        # 3. Альтернативные пулы той же пары для компенсации
        alternatives = [
            p
            for p in snapshot.pools_with_pair(token_in, token_out)
            if p.address != victim_pool.address and p.chain_id == ctx.chain_id
        ]
        if not alternatives:
            return None
        ref = max(alternatives, key=lambda p: p.tvl_usd)

        # 4. Компенсирующий цикл: token_out -> token_in на альтернативном пуле (реверс импакта)
        min_size = probe_amount(shifted, snapshot, token_out, self.min_size_usd)
        max_size = probe_amount(shifted, snapshot, token_out, self.max_size_usd)
        if min_size <= 0 or max_size <= min_size:
            return None

        def profit_fn(dx: int) -> float:
            mid = amount_out(shifted, token_out, dx)      # продаём обратно сдвинутый токен
            if mid <= 0:
                return float(-dx)
            return float(amount_out(ref, token_in, mid) - dx)

        size = ternary_search_optimal_size(profit_fn, min_size, max_size, iterations=40)
        out_amount = amount_out(ref, token_in, amount_out(shifted, token_out, size))
        gross = out_amount - size
        if gross <= 0:
            return None

        size_usd = snapshot.to_usd(token_out, size)
        profit_usd = snapshot.to_usd(token_out, gross)
        gas_usd = gas_cost_usd(
            gas_units=self.gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=float(self.param("priority_fee_gwei", 3.0)),
            native_price_usd=snapshot.native_price_usd,
        )
        flash_fee_usd = size_usd * Decimal(5) / Decimal(10_000)
        net = profit_usd - gas_usd - flash_fee_usd
        if net < self.min_profit_usd:
            return None
        # Теоретический потолок арбитража после свопа: нельзя «вытащить» больше половины
        # стоимости сдвига, созданного жертвой (иначе это признак кривой модели).
        impact_bound_usd = swap_usd * Decimal(str(impact)) / Decimal(10_000) * Decimal("0.5")
        if net > impact_bound_usd:
            log.debug(
                "backrun: net %s > impact bound %s — отклонено как нереалистичное",
                net,
                impact_bound_usd,
            )
            return None

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=token_out,
            token_out=token_in,
            size_in=size,
            expected_out=out_amount,
            profit_value=Decimal(gross),
            profit_usd=profit_usd,
            gas_usd=gas_usd,
            flash_fee_usd=flash_fee_usd,
            confidence=min(0.9, 0.45 + impact / 100 * 0.4 + min(float(net) / 1000, 0.15)),
            block_number=ctx.block_number,
            dex_buy=victim_pool.dex,
            dex_sell=ref.dex,
            pools=[victim_pool.address, ref.address],
            tokens=[token_in, token_out],
            kind="backrun",
            meta={
                "size_usd": str(size_usd),
                "victim_swap_usd": str(swap_usd),
                "victim_impact_bps": round(impact, 2),
                "sandwich": False,
                "roi_pct": float(net / size_usd * 100) if size_usd > 0 else 0.0,
            },
        )

    def _impact_bps(
        self, before: PoolSnapshot, after: PoolSnapshot, token_in: str, token_out: str
    ) -> float:
        """Ценовой импакт свопа в bps.

        Считаем по ИЗМЕНЕНИЮ СПОТ-КУРСА пула (у V3 — из sqrt_price_x96, у V2/Curve — из
        резервов), а не по «пробным» свопам: для глубоких V3-пулов проба в 1 DAI давала
        нулевой сдвиг из-за округлений в Q96-арифметике.
        """
        before_rate = self._spot(before, token_in, token_out)
        after_rate = self._spot(after, token_in, token_out)
        if before_rate <= 0 or after_rate <= 0:
            return 0.0
        return abs(after_rate - before_rate) / before_rate * 10_000

    @staticmethod
    def _spot(pool: PoolSnapshot, token_in: str, token_out: str) -> float:
        """Спот-курс token_out за token_in (в raw-единицах пула: decimals не важны для отношения)."""
        if pool.kind == "v3" and pool.sqrt_price_x96 > 0:
            sqrt_price = Decimal(pool.sqrt_price_x96) / Decimal(1 << 96)
            price_1_per_0 = sqrt_price * sqrt_price
            zero_for_one = pool.token0 == token_in
            rate = price_1_per_0 if zero_for_one else 1 / price_1_per_0
            return float(rate)
        reserve_in = pool.reserve_of(token_in)
        reserve_out = pool.reserve_of(token_out)
        if reserve_in <= 0 or reserve_out <= 0:
            return 0.0
        return float(Decimal(reserve_out) / Decimal(reserve_in))
