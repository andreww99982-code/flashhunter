"""Базовые типы стратегического слоя: контекст, Opportunity, интерфейс Strategy.

Интерфейс повторяет ТЗ п.3.1: on_start / on_stop / on_block / on_tx / on_oracle_update.
Стратегия — чистый вычислитель: она не отправляет сделки, а возвращает список
Opportunity, которые далее проходят validation -> simulation -> risk -> execution.
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from flashhunter.market.snapshot import (
    BorrowerPosition,
    CrossChainIntent,
    MarketSnapshot,
    PendingSwap,
)

StrategyName = Literal[
    "dex_arb", "triangular", "stable_peg", "liquidation", "jit", "backrun", "intent"
]


@dataclass(slots=True)
class BlockCtx:
    """Контекст нового блока."""

    chain_id: int
    block_number: int
    timestamp: float
    base_fee_gwei: float
    snapshot: MarketSnapshot


@dataclass(slots=True)
class TxCtx:
    """Контекст увиденной транзакции мемпула."""

    chain_id: int
    tx_hash: str
    pending: PendingSwap


@dataclass(slots=True)
class OracleUpdate:
    """Обновление оракула (Chainlink / Pyth / RedStone) — триггер для ликвидаций."""

    chain_id: int
    source: str
    asset: str
    price_usd: Decimal
    timestamp: float


@dataclass(slots=True)
class Opportunity:
    """Найденная возможность (кандидат на исполнение)."""

    strategy: StrategyName
    chain_id: int
    token_in: str
    token_out: str
    size_in: int                 # размер входа в минимальных единицах token_in
    expected_out: int            # ожидаемый выход в минимальных единицах token_out
    profit_value: Decimal        # прибыль в единицах token_in (до вычета газа)
    profit_usd: Decimal          # прибыль в USD
    gas_usd: Decimal
    flash_fee_usd: Decimal
    confidence: float
    block_number: int
    dex_buy: str = ""
    dex_sell: str = ""
    pools: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    kind: Literal["cycle", "liquidation", "jit", "backrun", "intent"] = "cycle"
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    # ------------------------------------------------------------------ #
    @property
    def net_profit_usd(self) -> Decimal:
        """Чистая прибыль: gross − gas − комиссия флеш-займа."""
        return self.profit_usd - self.gas_usd - self.flash_fee_usd

    @property
    def size_usd(self) -> Decimal:
        """Размер входа в USD (заполняется стратегией в meta при наличии цены)."""
        return Decimal(str(self.meta.get("size_usd", "0")))

    @property
    def roi_pct(self) -> float:
        """ROI в процентах от размера сделки."""
        size = self.size_usd
        if size <= 0:
            return 0.0
        return float(self.net_profit_usd / size * 100)

    def to_row(self) -> dict[str, Any]:
        """Плоское представление для БД/WS."""
        return {
            "uid": self.uid,
            "strategy": self.strategy,
            "chain_id": self.chain_id,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "dex_buy": self.dex_buy,
            "dex_sell": self.dex_sell,
            "size_usd": float(self.size_usd),
            "size_in": self.size_in,
            "expected_out": self.expected_out,
            "gross_profit_usd": float(self.profit_usd),
            "gas_usd": float(self.gas_usd),
            "flash_fee_usd": float(self.flash_fee_usd),
            "net_profit_usd": float(self.net_profit_usd),
            "roi_pct": round(self.roi_pct, 6),
            "confidence": round(self.confidence, 4),
            "block_number": self.block_number,
            "pools": self.pools,
            "kind": self.kind,
            "meta": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.meta.items()},
        }


class BaseStrategy(abc.ABC):
    """Базовый класс стратегии: реализует нетривиальные хуки, оставляя главный абстрактным."""

    name: StrategyName
    chains: list[int]
    enabled: bool = True

    def __init__(self, params: dict[str, Any] | None = None, chains: list[int] | None = None) -> None:
        self.params: dict[str, Any] = params or {}
        if chains:
            self.chains = chains

    # --- хуки жизненного цикла ------------------------------------------- #
    async def on_start(self) -> None:  # noqa: D401
        """Вызывается при включении стратегии (прогрев кэшей, подписки)."""
        return None

    async def on_stop(self) -> None:
        """Вызывается при выключении стратегии (освобождение ресурсов)."""
        return None

    @abc.abstractmethod
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Основной хук: поиск возможностей на новом блоке.

        Реализуется конкретной стратегией; вызов из оркестратора — асинхронный.
        """

    async def on_tx(self, ctx: TxCtx) -> list[Opportunity]:  # noqa: ARG002
        """Хук мемпула (по умолчанию ничего не делает; используется JIT/backrun)."""
        return []

    async def on_oracle_update(self, update: OracleUpdate) -> list[Opportunity]:  # noqa: ARG002
        """Хук обновления оракула (по умолчанию ничего не делает; используется ликвидациями)."""
        return []

    def on_block_sync(self, ctx: BlockCtx) -> list[Opportunity]:
        """Синхронный прогон on_block — нужен только backtest-движку.

        В live-режиме блокирующий вызов недопустим (остановит event loop),
        поэтому метод существует отдельно и оркестратором не используется.
        """
        import asyncio

        return asyncio.run(self.on_block(ctx))

    # --- хелперы ----------------------------------------------------------- #
    def param(self, key: str, default: Any = None) -> Any:
        """Параметр стратегии с дефолтом."""
        return self.params.get(key, default)

    def supports(self, chain_id: int) -> bool:
        """True, если стратегия работает в указанной сети."""
        return chain_id in self.chains

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"<{type(self).__name__} name={self.name} chains={self.chains} enabled={self.enabled}>"


def gas_cost_usd(
    *, gas_units: int, base_fee_gwei: float, priority_fee_gwei: float, native_price_usd: Decimal
) -> Decimal:
    """Стоимость газа в USD для заданного потребления газа (учитывает чаевые)."""
    gwei_total = Decimal(str(base_fee_gwei)) + Decimal(str(priority_fee_gwei))
    native = Decimal(gas_units) * gwei_total * Decimal(10) ** (-9)
    return native * native_price_usd


__all__ = [
    "BaseStrategy",
    "BlockCtx",
    "BorrowerPosition",
    "CrossChainIntent",
    "Opportunity",
    "OracleUpdate",
    "PendingSwap",
    "TxCtx",
    "gas_cost_usd",
]
