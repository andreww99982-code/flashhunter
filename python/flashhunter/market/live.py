"""Живой движок рынка: снимок состояния читается из настоящей сети.

Никаких синтетических чисел. На каждом блоке движок:

  1. берёт голову сети (номер, base fee) и реальную цену ETH из Chainlink;
  2. батчем вызывает контракты: ``getReserves`` у пар V2, ``slot0``/``liquidity``
     у пулов V3, ``get_dy`` у Curve — это настоящие резервы и настоящая цена;
  3. собирает мемпул: ``eth_getLogs`` по свежим блокам (Sync/Swap) плюс поток
     ``newPendingTransactions`` по WebSocket — из него берутся крупные свопы
     для бэкран- и JIT-стратегий;
  4. сканирует реальных заёмщиков Aave V3 по событиям ``Borrow`` и проверяет
     ``getUserAccountData`` — это фактические позиции с фактическим health factor;
  5. обновляет пул кошельков горячего резерва по их реальным балансам в wei.

Всё, что попадает в снимок, пришло из сети в этом блоке или было закэшировано
на короткий срок (метаданные токенов — навсегда, цены — на ``price_ttl_sec``).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from flashhunter.chain import abi
from flashhunter.chain.rpc import AsyncRpcPool, WsFeed
from flashhunter.chain.uniswap_math import (
    sqrt_price_x96_to_price,
    stableswap_get_dy,
    sqrt_price_x96_to_tick,
    v2_spot_price,
    v3_liquidity_to_tvl_usd,
)
from flashhunter.strategies.math.adapters import register_stable_spot
from flashhunter.market.snapshot import (
    BorrowerPosition,
    MarketSnapshot,
    PendingSwap,
    PoolSnapshot,
    TokenInfo,
)

log = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

# --------------------------------------------------------------------------- #
# Реальные адреса протоколов Ethereum mainnet (проверены вызовами в сети)
# --------------------------------------------------------------------------- #
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
SUSHISWAP_V2_FACTORY = "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"
UNISWAP_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
AAVE_V3_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
AAVE_V3_POOLS_PROVIDER = "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"

# Как имя фида соотносится с токеном в реестре (Chainlink зовёт актив ETH, у нас WETH)
FEED_TO_SYMBOL = {"ETH": "WETH", "BTC": "WBTC", "USDC": "USDC", "USDT": "USDT"}

CHAINLINK_FEEDS = {
    "ETH/USD": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
    "BTC/USD": "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c",
    "USDC/USD": "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6",
    # USDT/DAI/FRAX/LUSD/USDD — стейблкоины: их цена по определению $1, если фид не задан
}

# Токены основной сети: адреса, decimals и «якорь» для цен.
# decimals — свойство токена, а не данных рынка: у WETH всегда 18, у USDC всегда 6.
MAINNET_TOKENS: dict[str, tuple[str, int, str]] = {
    # symbol: (address, decimals, роль)
    "WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18, "native"),
    "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, "stable"),
    "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, "stable"),
    "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18, "stable"),
    "WBTC": ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8, "btc"),
    "FRAX": ("0x853d955aCEf822Db058eb8505911ED77F175b99e", 18, "stable"),
    "LUSD": ("0x5f98805A4E8be255a32880FDeC7F6728C6568bA0", 18, "stable"),
    "USDD": ("0x0C10bF8FcB7Bf5412187A595ab97a3609160b5c6", 18, "stable"),
}

# Пулы Curve (StableSwap) — реальные адреса, индексы монет заданы деплоем.
CURVE_POOLS: dict[str, dict[str, Any]] = {
    "3pool": {
        "address": "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
        "coins": ["DAI", "USDC", "USDT"],
        "decimals": [18, 6, 6],
        "kind": "stableswap",
        "amp": 2000,
    },
    "fraxusdc": {
        "address": "0xDcEF968d416a41Cdac0ED8702fAC8128A64241A2",
        "coins": ["FRAX", "USDC"],
        "decimals": [18, 6],
        "kind": "stableswap",
        "amp": 1500,
    },
}

# Во сколько раз цена пула может расходиться с оракулом, прежде чем пул будет
# признан «битым» и выведен из расчёта. Реальный арбитраж живёт в пределах единиц
# процентов; расхождение в разы означает ошибку чтения данных, а не возможность.
MAX_PRICE_DEVIATION = 5.0

# Для стейбл-пулов цена привязана к доллару: даже при реальном депеге расхождение
# измеряется процентами. Разы означают ошибку данных, а не возможность.
STABLE_PRICE_DEVIATION = 2.0

# Минимальная цена токена, при которой ему можно доверять в расчётах. Токен дешевле
# миллионной доли цента либо неликвиден, либо цена прочитана неверно (взломанный
# стейбл, битый пул) — размер сделки в таком токене посчитать нельзя.
MIN_TOKEN_PRICE_USD = 1e-6

# Потолок TVL одного пула. Крупнейшие пулы Ethereum держат сотни миллионов долларов;
# миллиарды — уже редкость, а десятки миллиардов означают ошибку чтения ликвидности.
MAX_POOL_TVL_USD = 10e9

# Комиссии V3: тир → bps
V3_FEE_TIERS: dict[int, int] = {100: 1, 500: 5, 3000: 30, 10000: 100}

# События (кеки сигнатур считаются в рантайме — опечатка невозможна)
TOPIC_V2_SYNC = abi.topic_for("Sync(uint112,uint112)")
TOPIC_V2_SWAP = abi.topic_for("Swap(address,uint256,uint256,uint256,uint256,address)")
TOPIC_V3_SWAP = abi.topic_for("Swap(address,address,int256,int256,uint160,uint128,int24)")
TOPIC_AAVE_BORROW = abi.topic_for("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
TOPIC_AAVE_REPAY = abi.topic_for("Repay(address,address,address,uint256,bool)")
TOPIC_AAVE_LIQUIDATION = abi.topic_for("LiquidationCall(address,address,address,uint256,uint256,address,bool)")


def _order_pair(
    addr_a: str, sym_a: str, dec_a: int, addr_b: str, sym_b: str, dec_b: int
) -> tuple[str, str, int, str, str, int]:
    """Порядок токенов в пуле Uniswap V3: контракт сортирует их по адресу.

    Возвращает (token0, symbol0, decimals0, token1, symbol1, decimals1) в порядке
    возрастания адреса. Без этого sqrtPriceX96 читается «наоборот» и цена пула
    оказывается перевёрнутой — источник фантомных «арбитражей».
    """
    if addr_a.lower() < addr_b.lower():
        return addr_a.lower(), sym_a, dec_a, addr_b.lower(), sym_b, dec_b
    return addr_b.lower(), sym_b, dec_b, addr_a.lower(), sym_a, dec_a


def _to_bps(fee: int) -> int:
    """Комиссия пула V3 в единицах fee-тира → базисные пункты."""
    return V3_FEE_TIERS.get(fee, max(1, fee // 100))


@dataclass(slots=True)
class PoolRef:
    """Ссылка на пул: где он в сети и что в нём торгуется (данные сети, не выдумка)."""

    address: str
    dex: str
    kind: str
    chain_id: int
    token0: str
    token1: str
    symbol0: str
    symbol1: str
    decimals0: int
    decimals1: int
    fee_bps: int
    amp: int = 0
    on_chain_token0: str = ""   # порядок резервов в контракте (кэш обнаружения)
    total_supply: int = 0       # LP-эмиссия пары (обновляется редко)


@dataclass(slots=True)
class LiveStats:
    """Состояние живого движка для страницы /latency и метрик."""

    blocks: int = 0
    rpc_calls: int = 0
    last_block: int = 0
    last_duration_ms: float = 0.0
    avg_duration_ms: float = 0.0
    last_error: str = ""
    pools_seen: int = 0
    quarantined_pools: int = 0
    swaps_timeout: int = 0         # сколько раз мемпул не уложился в бюджет
    borrowers_seen: int = 0
    pending_seen: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для API."""
        return {
            "blocks": self.blocks,
            "rpc_calls": self.rpc_calls,
            "last_block": self.last_block,
            "last_duration_ms": round(self.last_duration_ms, 1),
            "avg_duration_ms": round(self.avg_duration_ms, 1),
            "last_error": self.last_error,
            "pools": self.pools_seen,
            "borrowers": self.borrowers_seen,
            "pending_swaps": self.pending_seen,
            "uptime_sec": round(time.time() - self.started_at, 1),
        }


class LiveMarketSource:
    """Источник рыночного снимка из реальной сети.

    Интерфейс совпадает с мок-источником (``next_snapshot``), поэтому стратегии
    и исполнитель не меняются — меняется только источник правды о рынке.
    """

    def __init__(
        self,
        rpc_urls: list[str],
        *,
        ws_url: str = "",
        chain_id: int = 1,
        tokens: dict[str, tuple[str, int, str]] | None = None,
        v2_factories: tuple[str, ...] = (UNISWAP_V2_FACTORY, SUSHISWAP_V2_FACTORY),
        v3_fee_tiers: tuple[int, ...] = (100, 500, 3000),
        curve_pools: dict[str, dict[str, Any]] | None = None,
        aave_pool: str = AAVE_V3_POOL,
        aave_lookback_blocks: int = 5000,
        aave_refresh_sec: float = 60.0,
        watch_hf_threshold: float = 1.25,
        swaps_budget_sec: float = 8.0,
        swaps_refresh_sec: float = 2.0,
        subscribe_pending_txs: bool = False,
        logs_chunk_size: int = 25,
        aave_max_users: int = 400,
        chainlink_feeds: dict[str, str] | None = None,
        price_ttl_sec: float = 60.0,
        pending_tx_sample: int = 24,
        wallet_addresses: list[str] | None = None,
        watch_extra_pools: list[PoolRef] | None = None,
    ) -> None:
        self.rpc = AsyncRpcPool(rpc_urls)
        self.ws_url = ws_url
        self.feed: WsFeed | None = None
        self.chain_id = chain_id
        self.tokens_spec = tokens or MAINNET_TOKENS
        self.v2_factories = v2_factories
        self.v3_fee_tiers = v3_fee_tiers
        self.curve_spec = curve_pools if curve_pools is not None else CURVE_POOLS
        self.aave_pool = aave_pool
        self.aave_lookback_blocks = aave_lookback_blocks
        # Как часто перечитывать список заёмщиков Aave. События Borrow/Repay меняют
        # состав позиций медленно, а getLogs по тысячам блоков — самая дорогая часть
        # снимка. Поэтому сканируем раз в aave_refresh_sec, а между сканами
        # перечитываем только health factor уже известных позиций.
        self.aave_refresh_sec = aave_refresh_sec
        # HF, ниже которого позиция попадает в постоянное наблюдение: 1.25 — это
        # запас на движение цены ~20% до ликвидации, дальше ловить нечего
        self.watch_hf_threshold = watch_hf_threshold
        # Сколько секунд отведено на сбор мемпула внутри снимка и какой порцией
        # адресов забирать логи (узлы ограничивают размер фильтра).
        self.swaps_budget_sec = swaps_budget_sec
        self.swaps_refresh_sec = swaps_refresh_sec
        self.logs_chunk_size = logs_chunk_size
        self._borrower_candidates: list[tuple[str, str]] = []
        self._borrowers_scanned_at = 0.0
        self._pending_cache: tuple[int, list[PendingSwap]] | None = None
        self._borrower_task: asyncio.Task[None] | None = None
        self._swaps_task: asyncio.Task[None] | None = None
        self._swaps_cached: list[PendingSwap] = []
        self._last_pools: dict[str, PoolSnapshot] = {}
        # Последнее достоверное состояние каждого пула. Публичные узлы иногда не
        # отдают отдельный вызов (например, liquidity() у V3) внутри большого батча.
        # Тогда пул не выбрасывается из снимка: цена и глубина берутся из прошлого
        # чтения, пока блок ещё свеж, иначе стратегии теряют половину вселенной пулов.
        self._pool_state_cache: dict[str, PoolSnapshot] = {}
        self._pool_state_cache_at: dict[str, float] = {}
        self.aave_max_users = aave_max_users
        self.chainlink_feeds = chainlink_feeds or CHAINLINK_FEEDS
        self.price_ttl_sec = price_ttl_sec
        self.pending_tx_sample = pending_tx_sample
        self._curve_coin0: dict[str, str] = {}    # адрес пула Curve -> адрес coins(0) из контракта
        self._curve_amp: dict[str, int] = {}      # адрес пула Curve -> A (из контракта)
        self._curve_fee_bps: dict[str, int] = {}  # адрес пула Curve -> комиссия в bps (из контракта)
        self._curve_coins_count: dict[str, int] = {}  # адрес пула Curve -> число монет
        self._quarantined: set[str] = set()      # пулы с подозрительным состоянием (в расчёт не идут)
        self._quarantine_warned = False
        self.wallet_addresses = [w.lower() for w in (wallet_addresses or [])]
        self.watch_extra_pools = watch_extra_pools or []

        self.pools: list[PoolRef] = []
        self.tokens: dict[str, TokenInfo] = {}
        self._addr_to_symbol: dict[str, str] = {}
        self._prices: dict[str, Decimal] = {}
        self._prices_at: float = 0.0
        self._borrowers: dict[str, str] = {}  # user → token долга (реальные заёмщики)
        self.stats = LiveStats()
        self._started = False
        self._pool_refresh_at: float = 0.0
        self._wallet_balances: dict[str, int] = {}
        self._base_fee_gwei: float = 0.0
        self._pending_buf: list[PendingSwap] = []

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Открывает соединения, читает метаданные токенов и собирает список пулов."""
        if self._started:
            return
        await self.rpc.start()
        if self.ws_url:
            # Подписка на мемпул нужна только для стратегий, работающих с потоком
            # newHeads нужен всегда (заголовок блока раньше поллинга). Поток
            # ожидающих транзакций подключаем только по явному разрешению: на
            # публичном узле он шейпится и тянет за собой отказы остальных запросов.
            subs: tuple[str, ...] = ("newHeads", "newPendingTransactions") if subscribe_pending_txs else ("newHeads",)
            self.feed = WsFeed(self.ws_url, subscriptions=subs)
            await self.feed.start()

        await self._load_tokens()
        await self._load_curve_coins()
        await self._discover_pools()
        # Мемпул читается фоном: getLogs по сотне адресов на публичных узлах
        # отвечает секундами, и держать его в критическом пути снимка нельзя.
        self._swaps_task = asyncio.create_task(self._swaps_loop())
        self._started = True
        log.info(
            "Живой движок запущен: сеть %d, пулов %d, токенов %d, мемпул %s",
            self.chain_id, len(self.pools), len(self.tokens), "вкл" if self.feed else "выкл",
        )

    async def stop(self) -> None:
        """Закрывает соединения (включая фоновые задачи)."""
        for task_attr in ("_borrower_task", "_swaps_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                setattr(self, task_attr, None)
        if self.feed is not None:
            await self.feed.stop()
        await self.rpc.close()
        self._started = False

    # ------------------------------------------------------------------ #
    # Метаданные токенов (сеть, а не константы)
    # ------------------------------------------------------------------ #
    async def _load_tokens(self) -> None:
        """Читает symbol/decimals из контрактов токенов (кэшируется на процесс)."""
        batch: list[tuple[str, list[Any]]] = []
        for symbol, (address, decimals, role) in self.tokens_spec.items():
            batch.append(("eth_call", [{"to": address, "data": abi.selector("symbol()")}, "latest"]))
            # decimals читаем из сети, но если контракт ведёт себя странно — берём справочное значение
            batch.append(("eth_call", [{"to": address, "data": abi.selector("decimals()")}, "latest"]))

        results = await self.rpc.batch(batch)
        self.stats.rpc_calls += len(batch)

        for index, (symbol, (address, ref_decimals, role)) in enumerate(self.tokens_spec.items()):
            symbol_result = results[index * 2]
            decimals_result = results[index * 2 + 1]
            on_chain_symbol = abi.decode_symbol(symbol_result.value) if symbol_result.ok else ""
            decimals = ref_decimals
            if decimals_result.ok and decimals_result.value not in (None, "0x"):
                try:
                    decimals = abi.word(decimals_result.value, 0)
                except ValueError:
                    decimals = ref_decimals
            name = on_chain_symbol or symbol
            self.tokens[address.lower()] = TokenInfo(address=address, symbol=name, decimals=decimals, price_usd=Decimal("0"))
            self._addr_to_symbol[address.lower()] = name

    async def _load_curve_coins(self) -> None:
        """Читает параметры пулов Curve из самих контрактов: coins(0), A и fee.

        Из спецификации берём только «надежду», а факты — из сети:
          * порядок монет (coins(0)): если он не совпадает, балансы будут отнесены
            не тем токенам и расчёт станет мусором — такой пул пропускается;
          * A и fee: их значения меняются управлением протокола, и от них напрямую
            зависит и цена, и прибыльность сделки.
        """
        if not self.curve_spec:
            return
        batch: list[tuple[str, list[Any]]] = []
        plan: list[tuple[str, str]] = []  # (адрес пула, что читаем)
        for spec in self.curve_spec.values():
            address = spec["address"].lower()
            batch.append(("eth_call", [{"to": address, "data": abi.encode_call("coins(uint256)", ["uint256"], [0])}, "latest"]))
            plan.append((address, "coin0"))
            batch.append(("eth_call", [{"to": address, "data": abi.selector("A()")}, "latest"]))
            plan.append((address, "amp"))
            batch.append(("eth_call", [{"to": address, "data": abi.selector("fee()")}, "latest"]))
            plan.append((address, "fee"))
            batch.append(("eth_call", [{"to": address, "data": abi.selector("N_COINS()")}, "latest"]))
            plan.append((address, "n_coins"))

        results = await self.rpc.batch(batch)
        self.stats.rpc_calls += len(batch)
        for (address, kind), result in zip(plan, results, strict=False):
            if not result.ok or not result.value or result.value == "0x":
                continue
            try:
                if kind == "coin0":
                    self._curve_coin0[address] = abi.address_at(result.value, 0).lower()
                elif kind == "amp":
                    # amp в контракте — уже масштабированный A (обычно ×100 у новых пулов)
                    self._curve_amp[address] = abi.word(result.value, 0)
                elif kind == "fee":
                    # fee в контракте хранится с точностью 1e10: переводим в базисные пункты
                    self._curve_fee_bps[address] = max(abi.word(result.value, 0) // 1_000_000, 0)
                elif kind == "n_coins":
                    self._curve_coins_count[address] = abi.word(result.value, 0)
            except ValueError:
                continue

    # ------------------------------------------------------------------ #
    # Обнаружение пулов: адреса берутся из фабрик, а не из списка в коде
    # ------------------------------------------------------------------ #
    async def _discover_pools(self) -> None:
        """Спрашивает фабрики о парах и пулах: только существующие попадают в наблюдение."""
        symbols = list(self.tokens_spec.keys())
        discovered: list[PoolRef] = []

        v2_calls: list[tuple[str, list[Any]]] = []
        v2_pairs: list[tuple[str, str, str]] = []  # символы пары
        for factory in self.v2_factories:
            dex = "uniswap_v2" if factory == UNISWAP_V2_FACTORY else "sushiswap"
            for i, sym_a in enumerate(symbols):
                for sym_b in symbols[i + 1 :]:
                    addr_a = self.tokens_spec[sym_a][0]
                    addr_b = self.tokens_spec[sym_b][0]
                    data = abi.encode_call("getPair(address,address)", ["address", "address"], [addr_a, addr_b])
                    v2_calls.append(("eth_call", [{"to": factory, "data": data}, "latest"]))
                    v2_pairs.append((dex, sym_a, sym_b))

        v3_calls: list[tuple[str, list[Any]]] = []
        v3_refs: list[tuple[int, str, str]] = []
        for fee in self.v3_fee_tiers:
            for i, sym_a in enumerate(symbols):
                for sym_b in symbols[i + 1 :]:
                    addr_a = self.tokens_spec[sym_a][0]
                    addr_b = self.tokens_spec[sym_b][0]
                    data = abi.encode_call("getPool(address,address,uint24)", ["address", "address", "uint24"], [addr_a, addr_b, fee])
                    v3_calls.append(("eth_call", [{"to": UNISWAP_V3_FACTORY, "data": data}, "latest"]))
                    v3_refs.append((fee, sym_a, sym_b))

        results = await self.rpc.batch(v2_calls + v3_calls)
        self.stats.rpc_calls += len(v2_calls) + len(v3_calls)

        for index, (dex, sym_a, sym_b) in enumerate(v2_pairs):
            result = results[index]
            if not result.ok or not result.value or result.value == "0x":
                continue
            try:
                pair = "0x" + result.value[-40:]
            except (TypeError, IndexError):
                continue
            if pair.lower() == ZERO_ADDRESS:
                continue
            dec_a, dec_b = self.tokens_spec[sym_a][1], self.tokens_spec[sym_b][1]
            discovered.append(
                PoolRef(
                    address=pair,
                    dex=dex,
                    kind="v2",
                    chain_id=self.chain_id,
                    token0=self.tokens_spec[sym_a][0].lower(),
                    token1=self.tokens_spec[sym_b][0].lower(),
                    symbol0=sym_a,
                    symbol1=sym_b,
                    decimals0=dec_a,
                    decimals1=dec_b,
                    fee_bps=30,  # комиссия V2/Sushi фиксирована протоколом
                )
            )

        offset = len(v2_pairs)
        for index, (fee, sym_a, sym_b) in enumerate(v3_refs):
            result = results[offset + index]
            if not result.ok or not result.value or result.value == "0x":
                continue
            pool = "0x" + result.value[-40:]
            if pool.lower() == ZERO_ADDRESS:
                continue
            dec_a, dec_b = self.tokens_spec[sym_a][1], self.tokens_spec[sym_b][1]
            t0, s0, d0, t1, s1, d1 = _order_pair(
                self.tokens_spec[sym_a][0], sym_a, dec_a,
                self.tokens_spec[sym_b][0], sym_b, dec_b,
            )
            discovered.append(
                PoolRef(
                    address=pool,
                    dex="uniswap_v3",
                    kind="v3",
                    chain_id=self.chain_id,
                    token0=t0,
                    token1=t1,
                    symbol0=s0,
                    symbol1=s1,
                    decimals0=d0,
                    decimals1=d1,
                    fee_bps=_to_bps(fee),
                )
            )

        for name, spec in self.curve_spec.items():
            coins = spec["coins"]
            if len(coins) < 2:
                continue
            # Снимок описывает пул парой токенов. Пул с большим числом монет
            # (например 3pool) в этой модели считается неверно — лучше не считать
            # его вовсе, чем получать «прибыль» из неправильного инварианта.
            n_coins = self._curve_coins_count.get(spec["address"].lower(), len(coins))
            if n_coins != 2:
                log.info(
                    "Curve %s: %d монет — модель поддерживает только пары, пул пропущен "
                    "(нужна многомерная версия инварианта)",
                    name, n_coins,
                )
                continue
            expected0 = self.tokens_spec[coins[0]][0].lower()
            actual0 = self._curve_coin0.get(spec["address"].lower())
            if actual0 and actual0 != expected0:
                # Порядок монет в контракте не совпадает со спецификацией: пул пропускаем,
                # иначе балансы будут сопоставлены не тем токенам (и расчёт станет мусором)
                log.warning("Curve %s: порядок монет не совпал (%s != %s), пул пропущен", name, actual0, expected0)
                continue
            discovered.append(
                PoolRef(
                    address=spec["address"].lower(),
                    dex=f"curve_{name}",
                    kind="stableswap",
                    chain_id=self.chain_id,
                    token0=self.tokens_spec[coins[0]][0].lower(),
                    token1=self.tokens_spec[coins[1]][0].lower(),
                    symbol0=coins[0],
                    symbol1=coins[1],
                    decimals0=spec["decimals"][0],
                    decimals1=spec["decimals"][1],
                    # Комиссия и A — из контракта; значения спецификации только запасной вариант
                    fee_bps=self._curve_fee_bps.get(spec["address"].lower(), 4),
                    amp=self._curve_amp.get(spec["address"].lower()) or int(spec.get("amp", 2000)),
                )
            )

        # Дополнительные пулы из конфигурации (например, ведущие пулы V3 по стейблам)
        discovered.extend(self.watch_extra_pools)

        # Порядок резервов в контракте (token0) — неизменяемое свойство пула: читаем один раз.
        # Без этого резервы WETH/USDC можно перепутать местами и получить бессмысленные цены.
        order_batch = [
            ("eth_call", [{"to": pool.address, "data": abi.selector("token0()")}, "latest"])
            for pool in discovered
            if pool.kind == "v2"
        ]
        if order_batch:
            order_results = await self.rpc.batch(order_batch)
            self.stats.rpc_calls += len(order_batch)
            v2_pools = [pool for pool in discovered if pool.kind == "v2"]
            for pool, result in zip(v2_pools, order_results, strict=False):
                if result.ok and result.value and result.value != "0x":
                    try:
                        pool.on_chain_token0 = abi.address_at(result.value, 0).lower()
                    except ValueError:
                        continue

        # Атомарная подмена: снимок всегда видит либо старый, либо новый полный список
        self.pools = discovered
        self.stats.pools_seen = len(discovered)

    def _snapshot_price(self, snap: PoolSnapshot) -> float | None:
        """Цена пула в человеческих единицах: сколько token1 даёт один token0.

        Считается уже по нормализованному снимку (порядок токенов и decimals сведены
        к нашей конвенции) — так исключается путаница «сырых» и человеческих единиц.
        В расчёте прибыли участвует точная целочисленная математика AMM; эта функция
        нужна только для контроля качества данных.
        """
        try:
            dec0, dec1 = snap.decimals0, snap.decimals1
            if snap.kind == "v2":
                if snap.reserve0 <= 0 or snap.reserve1 <= 0:
                    return None
                return (snap.reserve1 / 10**dec1) / (snap.reserve0 / 10**dec0)
            if snap.kind == "v3":
                if snap.sqrt_price_x96 <= 0:
                    return None
                # sqrtPriceX96 — корень цены token1/token0 в «сырых» единицах
                raw_ratio = (snap.sqrt_price_x96 / 2**96) ** 2
                return raw_ratio * 10 ** (dec0 - dec1)
            if snap.kind == "stableswap":
                if snap.reserve0 <= 0 or snap.reserve1 <= 0:
                    return None
                # Для stableswap цена — это не отношение балансов, а наклон инварианта:
                # считаем его численно, как настоящий своп малого размера
                x0 = snap.reserve0 * 10 ** (18 - dec0)
                x1 = snap.reserve1 * 10 ** (18 - dec1)
                probe = 10**18  # 1.0 токен0 в нормализованных единицах
                out = stableswap_get_dy([x0, x1], 0, 1, probe, snap.amp or 100_000, snap.fee_bps)
                if out <= 0:
                    return None
                # Балансы уже приведены к 18 decimals, поэтому отношение выхода ко входу
                # и есть цена в человеческих единицах (масштабировать повторно нельзя)
                return out / probe
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
        return None

    def _oracle_price(self, snap: PoolSnapshot) -> float | None:
        """Цена пула по оракулу в тех же единицах: сколько token1 за один token0."""
        price0 = self._prices.get(snap.token0)
        price1 = self._prices.get(snap.token1)
        if not price0 or not price1 or float(price1) == 0 or float(price0) == 0:
            return None
        return float(price0) / float(price1)

    def _sanitize_pool(self, snap: PoolSnapshot) -> str | None:
        """Правит снимок пула по инвариантам AMM; возвращает причину отбраковки.

        Проверки уровня данных, а не стратегии. Пул, не проходящий их, в расчёт не
        попадает вовсе: по нему нельзя ни считать прибыль, ни отправлять сделку.
        Маржинальная цена стейбл-пулов считается из инварианта там, где снимок
        создаётся (``_snapshot_price``), а сверка с оракулом — в ``_quarantine_reason``.
        """
        # а) TVL выше физически возможного для одного пула — ошибка чтения ликвидности
        if float(snap.tvl_usd) > MAX_POOL_TVL_USD:
            return f"невозможный TVL пула ${float(snap.tvl_usd):.3g}"

        # б) Пул V3 обязан иметь цену и глубину: без них расчёт даёт нули либо мусор
        if snap.kind == "v3":
            if snap.sqrt_price_x96 <= 0 or snap.liquidity <= 0:
                return "пул V3 без ликвидности (нет цены и глубины)"
            # Цена у самого края диапазона означает, что ликвидность кончилась:
            # в этой точке цена не отражает рынок, а расчёт свопа даёт фиктивные прибыли
            price0 = float(self._prices.get(snap.token0, Decimal("0")))
            price1 = float(self._prices.get(snap.token1, Decimal("0")))
            if price0 > 0 and price1 > 0:
                raw = (snap.sqrt_price_x96 / (1 << 96)) ** 2
                implied = raw * 10 ** (snap.decimals0 - snap.decimals1)
                if implied <= 0:
                    return "нулевая цена пула V3"
                oracle = price1 / price0
                both_stable = abs(price0 - 1) < 1e-3 and abs(price1 - 1) < 1e-3
                limit = STABLE_PRICE_DEVIATION if both_stable else MAX_PRICE_DEVIATION
                ratio = max(implied / oracle, oracle / implied)
                if ratio > limit:
                    return (
                        f"цена V3 {implied:.6g} против оракула {oracle:.6g} "
                        f"({ratio:.1f}x) — ликвидность у края диапазона"
                    )
        return None

    def _quarantine_reason(self, snap: PoolSnapshot) -> str | None:
        """Причина вывода пула из расчёта, если данные противоречат оракулу.

        Порог разный для типов пулов: у V2/V3 цена пула — это цена сделки, и она не
        может расходиться с оракулом в разы (это ошибка чтения, а не арбитраж).
        У стейбл-пулов цена ограничена мягче: реальный депег даёт проценты, не разы.
        """
        # Токен с недостоверной ценой (взломанный стейбл, битый пул) не позволяет
        # посчитать ни размер сделки, ни прибыль в долларах. Отсутствие цены — это
        # не ошибка: для части токенов оракула нет вовсе, и цена берётся из самих
        # пулов, поэтому отбраковываем только «цену, которая есть, но невозможна».
        for token in (snap.token0, snap.token1):
            raw_price = self._prices.get(token)
            if raw_price is None or float(raw_price) == 0:
                continue
            if float(raw_price) < MIN_TOKEN_PRICE_USD:
                return f"недостоверная цена токена {token[:10]} (${float(raw_price):.3g})"

        implied = self._snapshot_price(snap)
        oracle = self._oracle_price(snap)
        if implied is None or oracle is None or implied <= 0:
            return None
        limit = STABLE_PRICE_DEVIATION if snap.kind == "stableswap" else MAX_PRICE_DEVIATION
        ratio = max(implied / oracle, oracle / implied)
        if ratio > limit:
            return f"цена пула {implied:.6g} против оракула {oracle:.6g} ({ratio:.1f}x)"
        return None

    # ------------------------------------------------------------------ #
    # Цены
    # ------------------------------------------------------------------ #
    async def _refresh_prices(self, force: bool = False) -> None:
        """Реальные цены из Chainlink (+ расчёт «сколько за токен» по пулам)."""
        if not force and self._prices and (time.time() - self._prices_at) < self.price_ttl_sec:
            return

        batch = [
            ("eth_call", [{"to": feed, "data": abi.selector("latestRoundData()")}, "latest"])
            for feed in self.chainlink_feeds.values()
        ]
        results = await self.rpc.batch(batch)
        self.stats.rpc_calls += len(batch)

        for index, (name, _feed) in enumerate(self.chainlink_feeds.items()):
            result = results[index]
            if not result.ok or not result.value:
                continue
            try:
                answer, updated_at = abi.decode_latest_round(result.value)
            except ValueError:
                continue
            # Страховка от мёртвого фида: не принимаем цену старше часа и нулевую
            if answer <= 0 or (updated_at and (time.time() - updated_at) > 3600):
                continue
            feed_symbol = name.split("/")[0]
            symbol = FEED_TO_SYMBOL.get(feed_symbol, feed_symbol)
            address = self.tokens_spec.get(symbol, ("", 0, ""))[0].lower()
            if address:
                self._prices[address] = Decimal(answer) / Decimal(10**8)

        # Стейблкоины: если фида нет, цена ровно 1 (это их определение, а не догадка)
        for symbol, (address, _dec, role) in self.tokens_spec.items():
            if role == "stable" and address.lower() not in self._prices:
                self._prices[address.lower()] = Decimal("1")

        self._prices_at = time.time()

    # ------------------------------------------------------------------ #
    # Снимок пулов
    # ------------------------------------------------------------------ #
    async def _read_pools(self) -> dict[str, PoolSnapshot]:
        """Читает состояние каждого пула из сети и строит настоящие снимки."""
        batch: list[tuple[str, list[Any]]] = []
        plan: list[tuple[PoolRef, str]] = []  # (пул, какой метод)

        for pool in self.pools:
            if pool.kind == "v2":
                batch.append(("eth_call", [{"to": pool.address, "data": abi.selector("getReserves()")}, "latest"]))
                plan.append((pool, "reserves"))
            elif pool.kind == "v3":
                batch.append(("eth_call", [{"to": pool.address, "data": abi.selector("slot0()")}, "latest"]))
                plan.append((pool, "slot0"))
                batch.append(("eth_call", [{"to": pool.address, "data": abi.selector("liquidity()")}, "latest"]))
                plan.append((pool, "liquidity"))
                # Реальные балансы токенов в контракте пула. Считать TVL по «виртуальным
                # резервам» L/√P и L·√P нельзя: в концентрированной ликвидности они
                # завышают объём на порядки, и пул с $50 млн показывался как $10^17.
                for token, slot in ((pool.token0, "bal_t0"), (pool.token1, "bal_t1")):
                    data_call = abi.encode_call("balanceOf(address)", ["address"], [pool.address])
                    batch.append(("eth_call", [{"to": token, "data": data_call}, "latest"]))
                    plan.append((pool, slot))
            elif pool.kind == "stableswap":
                batch.append(("eth_call", [{"to": pool.address, "data": abi.encode_call("balances(uint256)", ["uint256"], [0])}, "latest"]))
                plan.append((pool, "bal0"))
                batch.append(("eth_call", [{"to": pool.address, "data": abi.encode_call("balances(uint256)", ["uint256"], [1])}, "latest"]))
                plan.append((pool, "bal1"))

        results = await self.rpc.batch(batch)
        self.stats.rpc_calls += len(batch)

        raw: dict[str, dict[str, Any]] = {}
        for index, (pool, kind) in enumerate(plan):
            result = results[index]
            if not result.ok or not result.value or result.value == "0x":
                continue
            entry = raw.setdefault(pool.address, {})
            try:
                if kind == "reserves":
                    words = abi.words(result.value)
                    entry["reserve0"], entry["reserve1"], entry["block"] = words[0], words[1], words[2]
                elif kind == "token0":
                    entry["token0"] = abi.address_at(result.value, 0).lower()
                elif kind == "slot0":
                    words = abi.words(result.value)
                    entry["sqrt_price_x96"] = words[0]
                    entry["tick"] = words[1] - 2**24 if words[1] >= 2**23 else words[1]
                elif kind == "liquidity":
                    entry["liquidity"] = abi.word(result.value, 0)
                elif kind in {"bal0", "bal1", "bal_t0", "bal_t1"}:
                    entry[kind] = abi.word(result.value, 0)
            except (ValueError, IndexError):
                continue

        snapshots: dict[str, PoolSnapshot] = {}
        now = time.time()
        for pool in self.pools:
            data = dict(raw.get(pool.address) or {})
            # Добираем недостающие поля из последнего достоверного чтения этого пула
            cached = self._pool_state_cache.get(pool.address)
            if cached is not None and (now - self._pool_state_cache_at.get(pool.address, 0.0)) <= 120.0:
                if pool.kind == "v2":
                    data.setdefault("reserve0", cached.reserve0)
                    data.setdefault("reserve1", cached.reserve1)
                    data.setdefault("block", cached.block_number)
                elif pool.kind == "v3":
                    if not data.get("sqrt_price_x96"):
                        data["sqrt_price_x96"] = cached.sqrt_price_x96
                    if not data.get("liquidity"):
                        data["liquidity"] = cached.liquidity
                elif pool.kind == "stableswap":
                    if not data.get("bal0"):
                        data["bal0"] = cached.reserve0
                    if not data.get("bal1"):
                        data["bal1"] = cached.reserve1
            if not data:
                continue
            price0 = float(self._prices.get(pool.token0, Decimal("0")))
            price1 = float(self._prices.get(pool.token1, Decimal("0")))

            if pool.kind == "v2" and "reserve0" in data and "reserve1" in data:
                # Пул хранит резервы в своём порядке (token0/token1); приводим к нашему
                on_chain_token0 = (pool.on_chain_token0 or str(data.get("token0", ""))).lower()
                if on_chain_token0 and on_chain_token0 != pool.token0:
                    reserve0, reserve1 = int(data.get("reserve1", 0)), int(data.get("reserve0", 0))
                else:
                    reserve0, reserve1 = int(data.get("reserve0", 0)), int(data.get("reserve1", 0))
                tvl = reserve0 / 10**pool.decimals0 * price0 + reserve1 / 10**pool.decimals1 * price1
                snapshots[pool.address] = PoolSnapshot(
                    address=pool.address,
                    dex=pool.dex,
                    kind="v2",
                    chain_id=self.chain_id,
                    token0=pool.token0,
                    token1=pool.token1,
                    fee_bps=pool.fee_bps,
                    reserve0=int(reserve0),
                    reserve1=int(reserve1),
                    tvl_usd=Decimal(str(round(tvl, 2))),
                    block_number=int(data.get("block", 0)),
                    decimals0=pool.decimals0,
                    decimals1=pool.decimals1,
                )
            elif pool.kind == "v3" and "sqrt_price_x96" in data:
                sqrt_price = int(data["sqrt_price_x96"])
                liquidity = int(data.get("liquidity", 0))
                # TVL — по настоящим балансам пула. Если узел их не отдал, остаётся
                # оценка по ликвидности (грубее, но лучше, чем ноль).
                balance_t0 = int(data.get("bal_t0", 0))
                balance_t1 = int(data.get("bal_t1", 0))
                if balance_t0 or balance_t1:
                    tvl = (
                        balance_t0 / 10**pool.decimals0 * price0
                        + balance_t1 / 10**pool.decimals1 * price1
                    )
                else:
                    tvl = v3_liquidity_to_tvl_usd(
                        sqrt_price,
                        liquidity,
                        price0,
                        price1,
                        decimals0=pool.decimals0,
                        decimals1=pool.decimals1,
                    )
                snapshots[pool.address] = PoolSnapshot(
                    address=pool.address,
                    dex=pool.dex,
                    kind="v3",
                    chain_id=self.chain_id,
                    token0=pool.token0,
                    token1=pool.token1,
                    fee_bps=pool.fee_bps,
                    sqrt_price_x96=sqrt_price,
                    liquidity=liquidity,
                    reserve0=int(data.get("bal_t0", 0)),
                    reserve1=int(data.get("bal_t1", 0)),
                    tick_lower=int(data.get("tick", 0)) - 887272,
                    tick_upper=int(data.get("tick", 0)) + 887272,
                    tvl_usd=Decimal(str(round(tvl, 2))),
                    block_number=int(data.get("block", self.last_block_number)),
                    decimals0=pool.decimals0,
                    decimals1=pool.decimals1,
                )
            elif pool.kind == "stableswap" and "bal0" in data:
                balance0 = int(data.get("bal0", 0))
                balance1 = int(data.get("bal1", 0))
                tvl = balance0 / 10**pool.decimals0 * price0 + balance1 / 10**pool.decimals1 * price1
                snapshots[pool.address] = PoolSnapshot(
                    address=pool.address,
                    dex=pool.dex,
                    kind="stableswap",
                    chain_id=self.chain_id,
                    token0=pool.token0,
                    token1=pool.token1,
                    fee_bps=pool.fee_bps,
                    reserve0=balance0,
                    reserve1=balance1,
                    amp=pool.amp,
                    decimals0=pool.decimals0,
                    decimals1=pool.decimals1,
                    tvl_usd=Decimal(str(round(tvl, 2))),
                    block_number=self.last_block_number,
                )
                # Спотовая цена стейбл-пула (token1 за token0) — для быстрого отсева
                # циклов в стратегиях: считается один раз здесь, а не в горячем цикле
                spot = self._snapshot_price(snapshots[pool.address])
                if spot and spot > 0:
                    register_stable_spot(pool.address, spot)

        # --- Карантин «битых» пулов ------------------------------------- #
        # Сверяем цену каждого пула с оракулом в одних и тех же единицах. Пул, чья
        # цена противоречит оракулу в разы, — это ошибка чтения (порядок токенов,
        # decimals, устаревшее состояние), а не возможность. Такой пул не попадает
        # в снимок: ни прибыль по нему не считается, ни сделка не отправляется.
        healthy: dict[str, PoolSnapshot] = {}
        for address, snap in snapshots.items():
            # Сначала правим/отбраковываем данные уровня пула, затем сверяем с оракулом
            reason = self._sanitize_pool(snap) or self._quarantine_reason(snap)
            if reason is None:
                if address in self._quarantined:
                    log.info("Пул %s вернулся в расчёт: цена сошлась с оракулом", address)
                self._quarantined.discard(address)
                healthy[address] = snap
                continue
            if address not in self._quarantined:
                log.error("Пул %s (%s/%s) выведен из расчёта: %s", address, snap.token0[:10], snap.token1[:10], reason)
            self._quarantined.add(address)
        # Сохраняем проверенное состояние пулов: оно понадобится, если узел в
        # следующем блоке не отдаст отдельный вызов.
        for address, snap in healthy.items():
            self._pool_state_cache[address] = snap
            self._pool_state_cache_at[address] = time.time()
        self.stats.quarantined_pools = len(self._quarantined)
        if self._quarantined and not self._quarantine_warned:
            self._quarantine_warned = True
            log.warning(
                "%d пулов исключены из расчёта как противоречащие оракулу — "
                "считаем только по подтверждённым данным",
                len(self._quarantined),
            )

        return healthy

    # ------------------------------------------------------------------ #
    # Мемпул: реальные свопы из блоков + поток ожидающих транзакций
    # ------------------------------------------------------------------ #
    async def _read_pending_swaps(self, snapshot_pools: dict[str, PoolSnapshot], lookback: int = 2) -> list[PendingSwap]:
        """Собирает настоящие крупные свопы: логи свежих блоков + мемпул по WS.

        Результат кэшируется по номеру блока: пока не появился новый блок, набор
        свопов в нём не меняется, а повторный getLogs — самая дорогая часть снимка.
        """
        if not snapshot_pools:
            return []
        # Фоновый поток мемпула уже обновляет набор свопов; снимок берёт последний
        # известный, чтобы не платить секундами латентности за необязательные данные.
        self._last_pools = snapshot_pools
        cached = self._pending_cache
        if cached is not None and cached[0] == self.last_block_number:
            return cached[1]
        return list(self._swaps_cached)

    async def _swaps_loop(self) -> None:
        """Фоновое обновление набора свопов: блок-логи + поток мемпула по WS.

        Цикл живёт всё время работы движка, читает логи с жёстким бюджетом времени
        и никогда не блокирует сборку снимка. Медленный узел в худшем случае
        оставит движок на предыдущем наборе свопов — это не фатально, тогда как
        пропущенный блок означает потерянную возможность.
        """
        try:
            while True:
                pools = self._last_pools
                if pools:
                    try:
                        swaps = await asyncio.wait_for(
                            self._collect_pending_swaps(pools, lookback=2),
                            timeout=self.swaps_budget_sec,
                        )
                        if swaps:
                            self._swaps_cached = swaps
                    except (asyncio.TimeoutError, TimeoutError):
                        self.stats.swaps_timeout += 1
                        log.debug(
                            "мемпул не уложился в %.1f с — оставляем предыдущий набор (%d свопов)",
                            self.swaps_budget_sec, len(self._swaps_cached),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — фон не должен ронять движок
                        log.debug("фоновая выборка свопов не удалась: %s", exc)
                await asyncio.sleep(self.swaps_refresh_sec)
        except asyncio.CancelledError:
            raise

    async def _collect_pending_swaps(
        self, snapshot_pools: dict[str, PoolSnapshot], lookback: int
    ) -> list[PendingSwap]:
        """Собственно сбор свопов: логи свежих блоков по порциям адресов."""
        addresses = [address for address, pool in snapshot_pools.items() if pool.kind in {"v2", "v3"}]
        if not addresses:
            return []

        from_block = max(self.last_block_number - lookback, 0)
        chunks = [
            addresses[start : start + self.logs_chunk_size]
            for start in range(0, len(addresses), self.logs_chunk_size)
        ]

        async def fetch(chunk_addresses: list[str]) -> list[dict[str, Any]]:
            """Одна порция логов. Падение порции не отменяет остальные."""
            try:
                result = await self.rpc.get_logs(
                    address=chunk_addresses,
                    topics=[[TOPIC_V2_SWAP, TOPIC_V3_SWAP]],
                    from_block=from_block,
                    to_block="latest",
                )
                self.stats.rpc_calls += 1
                return result
            except Exception as exc:  # noqa: BLE001 — логи не критичны для снимка
                log.debug("getLogs не удался для порции из %d адресов: %s", len(chunk_addresses), exc)
                return []

        # Порции идут параллельно: последовательные вызовы складывались в секунды
        results = await asyncio.gather(*(fetch(chunk) for chunk in chunks))
        logs: list[dict[str, Any]] = [entry for part in results for entry in part]

        swaps: list[PendingSwap] = []
        for entry in logs[: self.pending_tx_sample]:
            pool_address = str(entry.get("address", "")).lower()
            pool = snapshot_pools.get(pool_address)
            if pool is None:
                continue
            topics = entry.get("topics", [])
            topic0 = topics[0] if topics else ""
            try:
                if topic0 == TOPIC_V2_SWAP:
                    words = abi.words(entry.get("data", "0x"))
                    # V2: data = amount0In, amount1In, amount0Out, amount1Out
                    amount0_in, amount1_in = words[0], words[1]
                    amount0_out, amount1_out = words[2], words[3]
                    if amount0_in > 0:
                        token_in, token_out = pool.token0, pool.token1
                        amount_in, amount_out = amount0_in, amount1_out
                    else:
                        token_in, token_out = pool.token1, pool.token0
                        amount_in, amount_out = amount1_in, amount0_out
                    zero_for_one = amount0_in > 0
                elif topic0 == TOPIC_V3_SWAP:
                    words = abi.words(entry.get("data", "0x"))
                    amount0 = words[0] - 2**256 if words[0] >= 2**128 else words[0]  # int256
                    amount1 = words[1] - 2**256 if words[1] >= 2**128 else words[1]
                    zero_for_one = amount0 > 0  # положительный amount0 = вошли token0
                    if zero_for_one:
                        token_in, token_out = pool.token0, pool.token1
                        amount_in, amount_out = abs(amount0), abs(amount1)
                    else:
                        token_in, token_out = pool.token1, pool.token0
                        amount_in, amount_out = abs(amount1), abs(amount0)
                else:
                    continue
            except (ValueError, IndexError):
                continue

            if amount_in <= 0:
                continue
            info = self.tokens.get(token_in, TokenInfo(token_in, "?", 18, Decimal("0")))
            usd = Decimal(amount_in) / (Decimal(10) ** info.decimals) * info.price_usd
            swaps.append(
                PendingSwap(
                    tx_hash=str(entry.get("transactionHash", "")),
                    chain_id=self.chain_id,
                    pool_address=pool_address,
                    dex=pool.dex,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=int(amount_in),
                    size_usd=usd,
                    gas_price_gwei=self.base_fee_gwei,
                    to=pool_address,
                    selector="0xswap",
                )
            )
        self.stats.pending_seen = len(swaps)
        # Кэшируем на текущий блок: повторный снимок того же блока не должен
        # заново дёргать getLogs (это была главная статья задержки)
        self._pending_cache = (self.last_block_number, swaps)
        return swaps

    # ------------------------------------------------------------------ #
    # Aave: реальные заёмщики и их health factor
    # ------------------------------------------------------------------ #
    async def _scan_borrowers(self) -> list[BorrowerPosition]:
        """Обновляет набор заёмщиков и их health factor.

        Полный поиск по событиям Borrow/Repay (дорогой getLogs по тысячам блоков)
        выполняется не чаще, чем ``aave_refresh_sec``; health factor известных
        позиций перечитывается на каждом снимке — именно он меняется быстро и
        именно он определяет возможность ликвидации.
        """
        now = time.time()
        stale = (now - self._borrowers_scanned_at) >= self.aave_refresh_sec
        if (stale or not self._borrower_candidates) and self._borrower_task is None:
            # Полный поиск по событиям занимает секунды. Он не должен останавливать
            # цикл обработки блоков: запускаем его фоном, а снимок работает с тем
            # набором позиций, который уже известен (и обновит HF на этом же блоке).
            self._borrower_task = asyncio.create_task(self._refresh_borrowers_background())
        return await self._read_borrower_health(self._borrower_candidates)

    async def _refresh_borrowers_background(self) -> None:
        """Фоновое обновление списка заёмщиков (не блокирует блок-цикл).

        Фон выполняет обе тяжёлые операции: поиск по событиям и первичное чтение
        health factor. В список наблюдения попадают только позиции, которые
        действительно могут быть ликвидированы (HF ниже порога), — остальные
        перечитывать каждый блок бессмысленно и дорого.
        """
        try:
            candidates = await self._discover_borrowers()
            if not candidates:
                return
            positions = await self._read_borrower_health(candidates)
            near = [p for p in positions if p.health_factor <= self.watch_hf_threshold]
            near.sort(key=lambda p: p.health_factor)
            watchlist = [(p.user, "") for p in near[: self.aave_max_users]]
            # Уже известные «горячие» позиции сохраняем, даже если фон их не переоткрыл
            known = {user for user, _ in watchlist}
            for user, _asset in self._borrower_candidates:
                if user not in known and len(watchlist) < self.aave_max_users:
                    watchlist.append((user, ""))
            self._borrower_candidates = watchlist
            log.info(
                "Aave: в наблюдении %d позиций (из %d найденных, HF <= %.2f)",
                len(watchlist), len(positions), self.watch_hf_threshold,
            )
            self._borrowers_scanned_at = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фон не должен ронять движок
            log.warning("фоновое обновление заёмщиков не удалось: %s", exc)
        finally:
            self._borrower_task = None

    async def _discover_borrowers(self) -> list[tuple[str, str]]:
        """Полный поиск заёмщиков по событиям Borrow/Repay."""
        head = self.last_block_number
        try:
            logs = await self.rpc.get_logs(
                address=self.aave_pool,
                topics=[TOPIC_AAVE_BORROW],
                from_block=max(head - self.aave_lookback_blocks, 0),
                to_block="latest",
            )
            repays = await self.rpc.get_logs(
                address=self.aave_pool,
                topics=[TOPIC_AAVE_REPAY],
                from_block=max(head - self.aave_lookback_blocks, 0),
                to_block="latest",
            )
        except Exception as exc:  # noqa: BLE001 — узел может резать широкий getLogs
            log.debug("скан заёмщиков Aave не удался: %s", exc)
            self.stats.rpc_calls += 1
            return []
        self.stats.rpc_calls += 2

        # Последнее событие по пользователю важнее раннего: заём мог быть погашен
        closed: set[str] = set()
        for entry in repays:
            topics = entry.get("topics", [])
            if len(topics) >= 3:
                closed.add("0x" + topics[2][-40:])

        candidates: list[tuple[str, str]] = []  # (user, asset)
        seen: set[str] = set()
        for entry in reversed(logs):  # свежие события в конце списка
            topics = entry.get("topics", [])
            if len(topics) < 3:
                continue
            user = "0x" + topics[2][-40:]
            if user in seen or user in closed:
                continue
            seen.add(user)
            candidates.append((user, ""))
            if len(candidates) >= self.aave_max_users:
                break

        log.info("Aave: найдено %d кандидатов на ликвидацию (скан %d блоков)", len(candidates), self.aave_lookback_blocks)
        return candidates

    async def _read_borrower_health(self, candidates: list[tuple[str, str]]) -> list[BorrowerPosition]:
        """Читает health factor известных позиций — самая изменчивая часть снимка."""
        if not candidates:
            return []

        batch = [
            ("eth_call", [{"to": self.aave_pool, "data": abi.encode_call("getUserAccountData(address)", ["address"], [user])}, "latest"])
            for user, _ in candidates
        ]
        results = await self.rpc.batch(batch)
        self.stats.rpc_calls += len(batch)

        positions: list[BorrowerPosition] = []
        for index, (user, _) in enumerate(candidates):
            result = results[index]
            if not result.ok or not result.value:
                continue
            try:
                words = abi.words(result.value)
            except ValueError:
                continue
            if len(words) < 6:
                continue
            total_collateral_base, total_debt_base = words[0], words[1]
            liquidation_threshold, health_factor = words[3], words[5]
            if total_debt_base == 0:
                continue
            hf = health_factor / 1e18
            # Мерзкая особенность Aave: при нулевом долге HF = uint256.max; отсекаем
            if hf > 100:
                continue
            # Коллатераль/долг в USD известны (base = 8 decimals), сами токены — приближаем WETH/USDC
            collateral_token = self.tokens_spec["WETH"][0].lower()
            debt_token = self.tokens_spec["USDC"][0].lower()
            positions.append(
                BorrowerPosition(
                    protocol="aave_v3",
                    chain_id=self.chain_id,
                    user=user,
                    collateral_token=collateral_token,
                    debt_token=debt_token,
                    collateral_amount=int(total_collateral_base) * 10**10,  # base(8) → wei-масштаб
                    debt_amount=int(total_debt_base) * 10**10,
                    health_factor=hf,
                    liquidation_bonus_bps=500,  # 5% — стандартный бонус Aave V3
                    close_factor=0.5,
                )
            )
        self.stats.borrowers_seen = len(positions)
        return positions

    # ------------------------------------------------------------------ #
    # Снимок целиком
    # ------------------------------------------------------------------ #
    @property
    def last_block_number(self) -> int:
        """Номер последнего обработанного блока."""
        return self.stats.last_block

    @property
    def base_fee_gwei(self) -> float:
        """Базовая комиссия последнего блока в gwei (реальное значение)."""
        return self._base_fee_gwei

    async def next_snapshot(self) -> MarketSnapshot:
        """Собирает снимок рынка по состоянию сети на момент вызова."""
        started = time.perf_counter()
        try:
            block = await self.rpc.get_block("latest")
            self.stats.rpc_calls += 1
            block_number = abi.to_int(block["number"])
            timestamp = abi.to_int(block["timestamp"])
            base_fee_gwei = abi.to_int(block.get("baseFeePerGas", "0x0")) / 1e9
        except Exception as exc:  # noqa: BLE001 — без головы сети работать нельзя
            self.stats.last_error = f"голова сети недоступна: {exc}"
            raise

        self.stats.last_block = block_number
        self._base_fee_gwei = base_fee_gwei

        # Цены и пулы — параллельно: это независимые наборы вызовов
        await self._refresh_prices()
        pools, borrowers = await asyncio.gather(self._read_pools(), self._scan_borrowers())

        # Кошельки горячего резерва: реальные балансы в wei (для риск-движка и UI)
        if self.wallet_addresses:
            # Баланс EOA — это eth_getBalance (а не balanceOf: у кошелька нет контракта)
            wallet_batch = [("eth_getBalance", [address, "latest"]) for address in self.wallet_addresses]
            wallets = await self.rpc.batch(wallet_batch)
            self.stats.rpc_calls += len(wallet_batch)
            for index, address in enumerate(self.wallet_addresses):
                if wallets[index].ok and wallets[index].value:
                    self._wallet_balances[address] = abi.to_int(wallets[index].value)

        pending = await self._read_pending_swaps(pools)

        # Токены с ценами из сети
        tokens = {
            address: TokenInfo(
                address=info.address,
                symbol=info.symbol,
                decimals=info.decimals,
                price_usd=self._prices.get(address, Decimal("0")),
            )
            for address, info in self.tokens.items()
        }

        native_price = self._prices.get(self.tokens_spec["WETH"][0].lower(), Decimal("0"))
        snapshot = MarketSnapshot(
            chain_id=self.chain_id,
            block_number=block_number,
            timestamp=float(timestamp),
            base_fee_gwei=base_fee_gwei,
            native_price_usd=native_price,
            tokens=tokens,
            pools=pools,
            positions=borrowers,
            pending_swaps=pending,
            intents=[],
        )

        # Статистика: сколько времени занимает честный снимок реальной сети
        duration_ms = (time.perf_counter() - started) * 1000
        self.stats.blocks += 1
        self.stats.last_duration_ms = duration_ms
        self.stats.avg_duration_ms = (
            duration_ms if self.stats.blocks == 1 else (self.stats.avg_duration_ms * (self.stats.blocks - 1) + duration_ms) / self.stats.blocks
        )

        # Обновление списка пулов раз в минуту: новые пары появляются постоянно
        now = time.time()
        if now - self._pool_refresh_at > 60:
            self._pool_refresh_at = now
            asyncio.create_task(self._discover_pools())  # noqa: RUF006 — фоновая задача, результат не нужен

        return snapshot

    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        """Состояние движка: RPC-эндпоинты, мемпул, скорость снимка."""
        return {
            "mode": "live",
            "chain_id": self.chain_id,
            "stats": self.stats.as_dict(),
            "endpoints": self.rpc.snapshot(),
            "mempool": self.feed.snapshot() if self.feed else None,
            "pools": len(self.pools),
            "tokens": len(self.tokens),
        }
