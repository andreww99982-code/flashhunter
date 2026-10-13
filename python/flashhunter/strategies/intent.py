"""Cross-chain arbitrage через интенты (Across, deBridge DLN, LayerZero OFT).

Схема: цена токена на dst-сети выше, чем на src (или наоборот). Мы формируем
интент на бридж, получая спред за вычетом комиссии бриджа. Учёт: fee_bps,
задержка ETA (риск изменения цены), риск незаполнения релеером.

Это не внутриблочная гонка: горизонт — минуты, поэтому стратегия оперирует
более крупными размерами и меньшим числом сделок.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from flashhunter.market.snapshot import CrossChainIntent
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity

log = logging.getLogger(__name__)


class IntentStrategy(BaseStrategy):
    """Поиск кросс-чейн расхождений цен с учётом комиссий бриджей."""

    name = "intent"  # type: ignore[assignment]
    chains = [1, 8453, 42161]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.max_bridge_fee_bps = int(self.param("max_bridge_fee_bps", 25))
        self.max_eta_sec = int(self.param("max_eta_sec", 900))
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 25.0)))
        self.min_size_usd = Decimal(str(self.param("min_size_usd", 5_000.0)))
        self.max_size_usd = Decimal(str(self.param("max_size_usd", 500_000.0)))
        self.bridges = set(self.param("bridges", ["across", "debridge_dln", "layerzero_oft"]))
        self.top_n = int(self.param("top_n", 3))

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Оценивает интенты, у которых цена назначения отличается от локальной."""
        snapshot = ctx.snapshot
        found: list[Opportunity] = []
        for intent in snapshot.intents:
            if intent.src_chain != ctx.chain_id or intent.bridge not in self.bridges:
                continue
            if intent.fee_bps > self.max_bridge_fee_bps or intent.eta_sec > self.max_eta_sec:
                continue
            opp = self._evaluate(ctx, intent)
            if opp is not None:
                found.append(opp)
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return found[: self.top_n]

    def _evaluate(self, ctx: BlockCtx, intent: CrossChainIntent) -> Opportunity | None:
        """Считает спред между локальной ценой и ценой на dst-сети за вычетом издержек."""
        snapshot = ctx.snapshot
        local_price = snapshot.price(intent.token_src)
        if local_price <= 0:
            return None
        size_usd = snapshot.to_usd(intent.token_src, intent.amount_in)
        if size_usd < self.min_size_usd or size_usd > self.max_size_usd:
            return None

        gross_usd = size_usd * (intent.dst_price_usd - local_price) / local_price
        if gross_usd <= 0:
            return None

        bridge_fee_usd = size_usd * Decimal(intent.fee_bps) / Decimal(10_000)
        # риск цены за время ETA: волатильность ~ 0.02% на 100 секунд ожидания
        eta_risk_usd = size_usd * Decimal(str(0.0002 * (intent.eta_sec / 100)))
        no_gas = Decimal("0")  # газ оплачивается на dst-сети и мал относительно размера
        net = gross_usd - bridge_fee_usd - eta_risk_usd
        if net < self.min_profit_usd:
            return None

        roi = float(net / size_usd * 100) if size_usd > 0 else 0.0
        # уверенность падает с ростом ETA и комиссии бриджа
        confidence = max(
            0.2, min(0.88, 0.75 - intent.eta_sec / 3600 - intent.fee_bps / 200 + min(roi, 1.0) * 0.2)
        )

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=intent.token_src,
            token_out=intent.token_dst,
            size_in=intent.amount_in,
            expected_out=intent.amount_in,
            profit_value=net,
            profit_usd=gross_usd,
            gas_usd=no_gas,
            flash_fee_usd=bridge_fee_usd,
            confidence=confidence,
            block_number=ctx.block_number,
            dex_buy=intent.bridge,
            dex_sell=intent.bridge,
            pools=[],
            tokens=[intent.token_src, intent.token_dst],
            kind="intent",
            meta={
                "size_usd": str(size_usd),
                "bridge": intent.bridge,
                "src_chain": intent.src_chain,
                "dst_chain": intent.dst_chain,
                "fee_bps": intent.fee_bps,
                "eta_sec": intent.eta_sec,
                "dst_price_usd": str(intent.dst_price_usd),
                "local_price_usd": str(local_price),
                "symbol_in": snapshot.token(intent.token_src).symbol,
                "roi_pct": roi,
            },
        )
