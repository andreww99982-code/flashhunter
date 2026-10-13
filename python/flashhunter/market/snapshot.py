"""Типы рыночного снимка (единый контракт между Rust-индексатором и Python-стратегиями).

В production снимок формируется Rust-сервисами ``pool-indexer`` / ``ingestor`` и
отдаётся по gRPC / NATS (см. proto/market.proto). В single-node демо-режиме
используется ``MockMarketSource`` — детерминированный генератор, сохраняющий
структуру настоящих данных (адреса пулов, резервы, спреды, HF позиций).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(slots=True)
class TokenInfo:
    """Метаданные токена: symbol/decimals + референсная цена в USD."""

    address: str
    symbol: str
    decimals: int
    price_usd: Decimal


@dataclass(slots=True)
class PoolSnapshot:
    """Состояние пула на момент снимка."""

    address: str
    dex: str
    kind: Literal["v2", "v3", "stableswap", "weighted"]
    chain_id: int
    token0: str
    token1: str
    fee_bps: int
    reserve0: int = 0          # для v2 / stableswap (балансы)
    reserve1: int = 0
    sqrt_price_x96: int = 0    # для v3
    liquidity: int = 0         # для v3
    tick_lower: int = -887272
    tick_upper: int = 887272
    amp: int = 0               # для stableswap (A * 100)
    tvl_usd: Decimal = Decimal("0")
    block_number: int = 0
    # decimals токенов пула: нужны для перевода «сырых» резервов в человеческие
    # единицы (контроль качества данных, отображение в UI)
    decimals0: int = 18
    decimals1: int = 18

    def reserve_of(self, token: str) -> int:
        """Резерв указанного токена (только для v2/stableswap)."""
        return self.reserve0 if token == self.token0 else self.reserve1


@dataclass(slots=True)
class BorrowerPosition:
    """Позиция заёмщика протокола кредитования (для ликвидаций)."""

    protocol: str
    chain_id: int
    user: str
    collateral_token: str
    debt_token: str
    collateral_amount: int          # в минимальных единицах
    debt_amount: int
    health_factor: float
    liquidation_bonus_bps: int
    close_factor: float = 0.5


@dataclass(slots=True)
class PendingSwap:
    """Крупный своп из мемпула (кандидат на backrun / JIT)."""

    tx_hash: str
    chain_id: int
    pool_address: str
    dex: str
    token_in: str
    token_out: str
    amount_in: int
    size_usd: Decimal
    gas_price_gwei: float
    to: str = ""
    selector: str = ""


@dataclass(slots=True)
class CrossChainIntent:
    """Интент кросс-чейн бриджа (Across / deBridge DLN / LayerZero OFT)."""

    bridge: str
    src_chain: int
    dst_chain: int
    token_src: str
    token_dst: str
    amount_in: int
    fee_bps: int
    eta_sec: int
    dst_price_usd: Decimal


@dataclass(slots=True)
class MarketSnapshot:
    """Полный снимок рынка для одного блока: всё, что видят стратегии."""

    chain_id: int
    block_number: int
    timestamp: float
    base_fee_gwei: float
    native_price_usd: Decimal
    tokens: dict[str, TokenInfo] = field(default_factory=dict)
    pools: dict[str, PoolSnapshot] = field(default_factory=dict)
    positions: list[BorrowerPosition] = field(default_factory=list)
    pending_swaps: list[PendingSwap] = field(default_factory=list)
    intents: list[CrossChainIntent] = field(default_factory=list)

    # --- Хелперы -----------------------------------------------------------
    def pools_with_pair(self, token_a: str, token_b: str) -> list[PoolSnapshot]:
        """Все пулы, в которых торгуется пара (в любом порядке), только на нужной сети."""
        out: list[PoolSnapshot] = []
        for pool in self.pools.values():
            if pool.chain_id != self.chain_id:
                continue
            pair = {pool.token0, pool.token1}
            if pair == {token_a, token_b}:
                out.append(pool)
        return out

    def token(self, address: str) -> TokenInfo:
        """Метаданные токена; для неизвестных — синтетические (18 decimals, цена 0)."""
        return self.tokens.get(address.lower(), TokenInfo(address, "UNKNOWN", 18, Decimal("0")))

    def price(self, address: str) -> Decimal:
        """Референсная цена токена в USD."""
        return self.token(address).price_usd

    def to_usd(self, address: str, amount: int) -> Decimal:
        """Перевод минимальных единиц токена в USD."""
        info = self.token(address)
        return Decimal(amount) / (Decimal(10) ** info.decimals) * info.price_usd

    def extra(self) -> dict[str, Any]:
        """Метаданные для логов/метрик."""
        return {
            "chain_id": self.chain_id,
            "block": self.block_number,
            "pools": len(self.pools),
            "positions": len(self.positions),
            "pending": len(self.pending_swaps),
        }
