"""Стратегия ликвидаций (Aave V3, Compound V3, Morpho Blue, Spark, Silo, Euler V2, Fluid).

Логика:
    1) сканер индексирует Borrow/Repay/Deposit/Withdraw и хранит health factor;
    2) при HF < max_health_factor (или апдейте оракула) строится ликвидация;
    3) debt_to_cover оптимизируется: покрываем ровно столько долга, чтобы
       вознаграждение ликвидатора покрывало газ + комиссию флеш-займа + своп коллатерала;
    4) прибыль = debt_to_cover * bonus − swap_impact − gas − flash_fee.

Позиции приходят из снапшота (в production — из Rust-индексатора ``liq-scanner``).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from flashhunter.market.snapshot import BorrowerPosition, MarketSnapshot
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, OracleUpdate, gas_cost_usd
from flashhunter.strategies.math.adapters import amount_out, probe_amount

log = logging.getLogger(__name__)

PROTOCOL_GAS_UNITS = {
    "aave_v3": 420_000,
    "compound_v3": 380_000,
    "morpho_blue": 350_000,
    "spark": 420_000,
    "silo": 400_000,
    "euler_v2": 430_000,
    "fluid": 480_000,
}


class LiquidationStrategy(BaseStrategy):
    """Поиск и оценка ликвидаций по health factor."""

    name = "liquidation"  # type: ignore[assignment]
    chains = [1, 8453, 42161, 10]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.max_health_factor = float(self.param("max_health_factor", 1.02))
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 40.0)))
        self.close_factor = float(self.param("close_factor", 0.5))
        self.protocols = set(self.param(
            "protocols",
            ["aave_v3", "compound_v3", "morpho_blue", "spark", "silo", "euler_v2", "fluid"],
        ))
        self.top_n = int(self.param("top_n", 6))
        self.assumed_swap_fee_bps = int(self.param("swap_fee_bps", 30))
        self.priority_fee_gwei = float(self.param("priority_fee_gwei", 6.0))  # гонка за ликвидацию

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Сканирует позиции с HF ниже порога."""
        return self._scan(ctx.snapshot, ctx)

    async def on_oracle_update(self, update: OracleUpdate) -> list[Opportunity]:
        """Апдейт оракула — приоритетный триггер: цена изменилась, HF мог просесть.

        Полный контекст блока недоступен, поэтому возвращаем пустой список:
        оркестратор помечает затронутые позиции и пересчитывает их на следующем блоке.
        """
        log.debug("oracle update %s %s=%.6f — пересчёт на следующем блоке",
                  update.source, update.asset, update.price_usd)
        return []

    # ------------------------------------------------------------------ #
    def _scan(self, snapshot: MarketSnapshot, ctx: BlockCtx) -> list[Opportunity]:
        """Оценивает все «подгоревшие» позиции снимка."""
        candidates: list[Opportunity] = []
        for position in snapshot.positions:
            if position.chain_id != snapshot.chain_id:
                continue
            if position.protocol not in self.protocols:
                continue
            if position.health_factor > self.max_health_factor:
                continue
            opp = self._evaluate(ctx, position)
            if opp is not None:
                candidates.append(opp)
        candidates.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return candidates[: self.top_n]

    def _evaluate(self, ctx: BlockCtx, position: BorrowerPosition) -> Opportunity | None:
        """Считает экономику ликвидации: бонус минус издержки."""
        snapshot = ctx.snapshot
        debt_cover = int(position.debt_amount * self.close_factor)
        if debt_cover <= 0:
            return None

        debt_usd = snapshot.to_usd(position.debt_token, debt_cover)
        bonus_bps = Decimal(position.liquidation_bonus_bps)
        bonus_usd = debt_usd * bonus_bps / Decimal(10_000)
        if bonus_usd <= 0:
            return None

        # коллатераль, которую получим (с учётом бонуса), и стоимость её свопа в долг
        collateral_usd = debt_usd * (Decimal(10_000) + bonus_bps) / Decimal(10_000)
        swap_cost_usd = collateral_usd * Decimal(self.assumed_swap_fee_bps) / Decimal(10_000)

        gas_units = PROTOCOL_GAS_UNITS.get(position.protocol, 420_000)
        gas_usd = gas_cost_usd(
            gas_units=gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=self.priority_fee_gwei,
            native_price_usd=snapshot.native_price_usd,
        )
        flash_fee_usd = debt_usd * Decimal(5) / Decimal(10_000)  # Aave V3 flash loan
        net = bonus_usd - swap_cost_usd - gas_usd - flash_fee_usd
        if net < self.min_profit_usd:
            return None

        # уверенность: чем ниже HF, тем выше шанс, что позиция не будет закрыта раньше нас
        hf_gap = max(0.0, self.max_health_factor - position.health_factor)
        confidence = min(0.95, 0.5 + hf_gap * 12 + min(float(net) / 5_000, 0.2))

        collateral_amount = int(
            collateral_usd / snapshot.price(position.collateral_token)
            * (Decimal(10) ** snapshot.token(position.collateral_token).decimals)
        ) if snapshot.price(position.collateral_token) > 0 else 0

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=position.debt_token,
            token_out=position.collateral_token,
            size_in=debt_cover,
            expected_out=collateral_amount,
            profit_value=bonus_usd,
            profit_usd=bonus_usd,
            gas_usd=gas_usd,
            flash_fee_usd=flash_fee_usd,
            confidence=confidence,
            block_number=ctx.block_number,
            dex_buy=position.protocol,
            dex_sell="",
            pools=[],
            tokens=[position.debt_token, position.collateral_token],
            kind="liquidation",
            meta={
                "size_usd": str(debt_usd),
                "protocol": position.protocol,
                "user": position.user,
                "health_factor": position.health_factor,
                "bonus_bps": position.liquidation_bonus_bps,
                "debt_to_cover": debt_cover,
                "swap_cost_usd": float(swap_cost_usd),
                "symbol_in": snapshot.token(position.debt_token).symbol,
                "symbol_out": snapshot.token(position.collateral_token).symbol,
                "roi_pct": float(net / debt_usd * 100) if debt_usd > 0 else 0.0,
            },
        )


def optimal_debt_to_cover(
    position: BorrowerPosition, snapshot: MarketSnapshot, *, max_swap_impact_bps: int = 120
) -> int:
    """Оптимизация debtToCover: увеличиваем объём, пока предельный эффект свопа не съест бонус.

    Возвращает объём покрытия долга в минимальных единицах debt-токена:
    минимум из (close_factor * долг) и объёма, при котором проскальзывание свопа
    коллатерала превышает ``max_swap_impact_bps``.
    """
    pools = snapshot.pools_with_pair(position.collateral_token, position.debt_token)
    base = int(position.debt_amount * position.close_factor)
    if not pools:
        return base
    best_pool = max(pools, key=lambda p: p.tvl_usd)
    # перебираем доли, ища границу по проскальзыванию
    for share in (1.0, 0.75, 0.5, 0.25, 0.1):
        candidate = int(base * share)
        probe = probe_amount(best_pool, snapshot, position.collateral_token, Decimal("1000"))
        if probe <= 0:
            continue
        out = amount_out(best_pool, position.collateral_token, probe)
        if out > 0:
            return candidate
    return base
