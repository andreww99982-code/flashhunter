"""Детерминированный генератор рыночного снимка (демо/CI-режим без нод).

Генерирует связный рынок: несколько пар, по 3–6 пулов на пару (Uniswap V2/V3,
Sushi, Pancake, Curve, Balancer), с реалистичными TVL, комиссиями и небольшими
взаимными отклонениями цен — так возникают настоящие арбитражные циклы.
Профили волатильности: calm / normal / volatile / stress.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from decimal import Decimal

from flashhunter.market.snapshot import (
    BorrowerPosition,
    CrossChainIntent,
    MarketSnapshot,
    PendingSwap,
    PoolSnapshot,
    TokenInfo,
)
from flashhunter.strategies.math.amm import Q96

# Множитель глубины V3 относительно эквивалентного V2-пула: концентрированная
# ликвидность даёт на том же капитале в 2–10 раз большую глубину у текущей цены.
V3_DEPTH_MULTIPLIER = 3.0
# Для стейбл-пар диапазон концентрируется в нескольких bps вокруг паритета
# (реальные пулы USDC/USDT 0.01% держат сотни миллионов при L на порядки выше).
V3_STABLE_DEPTH_MULTIPLIER = 25.0

# Базовые пары: (token0_symbol, token1_symbol, цена token0 в USD)
BASE_PAIRS: list[tuple[str, str, float]] = [
    ("WETH", "USDC", 2650.0),
    ("WBTC", "USDC", 61000.0),
    ("WETH", "USDT", 2650.0),
    ("DAI", "USDC", 1.0),
    ("USDC", "USDT", 1.0),
]

# Каталог пулов: для «обычных» пар и для стейблкоин-пар наборы разные —
# на стейблах реальные протоколы держат пулы с комиссией 1–4 bps (Curve, V3 1bp).
DEX_CATALOG: list[tuple[str, str, int]] = [
    ("uniswap_v2", "v2", 30),
    ("sushiswap", "v2", 30),
    ("uniswap_v3", "v3", 5),
    ("pancakeswap_v3", "v3", 10),
    ("balancer", "weighted", 30),
]

STABLE_DEX_CATALOG: list[tuple[str, str, int]] = [
    ("uniswap_v2", "v2", 30),
    ("sushiswap", "v2", 30),
    ("uniswap_v3", "v3", 1),
    ("curve_3pool", "stableswap", 1),
    ("curve_ng", "stableswap", 4),
]

TOKEN_DECIMALS: dict[str, int] = {"WETH": 18, "WBTC": 8, "USDC": 6, "USDT": 6, "DAI": 18}

VOLATILITY_PROFILES: dict[str, tuple[float, float]] = {
    # профиль: (множитель спреда между пулами, вероятность экстремального отклонения)
    "calm": (0.0006, 0.02),
    "normal": (0.0018, 0.08),
    "volatile": (0.0055, 0.22),
    "stress": (0.0120, 0.45),
}


def _addr(seed: str) -> str:
    """Детерминированный «адрес» из строки (20 байт, hex)."""
    digest = hashlib.sha3_256(seed.encode()).hexdigest()[:40]
    return "0x" + digest


@dataclass(slots=True)
class MockMarketSource:
    """Источник снимков для демо-режима.

    seed фиксирован -> воспроизводимость (используется в тестах и backtest).
    drift_step — доля дрейфа цены за блок (эмуляция движения рынка).
    """

    chain_id: int = 1
    seed: int = 1337
    profile: str = "normal"
    tokens_usd: dict[str, float] | None = None
    drift_step: float = 0.0007
    _block: int = 0
    _rng: random.Random = None  # type: ignore[assignment]
    _prices: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        # полный набор референсных цен: majors из BASE_PAIRS + стейблы
        defaults: dict[str, float] = {"WETH": 2650.0, "WBTC": 61000.0, "USDC": 1.0, "USDT": 1.0, "DAI": 1.0}
        defaults.update(self.tokens_usd or {})
        self._prices = defaults

    # ------------------------------------------------------------------ #
    def next_snapshot(self, block_number: int | None = None) -> MarketSnapshot:
        """Формирует снимок следующего блока: цены дрейфуют, пулы отклоняются друг от друга."""
        self._block = block_number if block_number is not None else self._block + 1
        rng = self._rng
        spread, tail_prob = VOLATILITY_PROFILES.get(self.profile, VOLATILITY_PROFILES["normal"])

        # 1. Дрейф «истинных» цен (общий рынок)
        for symbol, price in self._prices.items():
            if symbol in {"USDC", "USDT", "DAI"}:
                self._prices[symbol] = price + rng.gauss(0, 0.00005) * price
            else:
                self._prices[symbol] = price * (1 + rng.gauss(0, self.drift_step))

        tokens: dict[str, TokenInfo] = {}
        for symbol, price in self._prices.items():
            tokens[symbol] = TokenInfo(
                address=_addr(f"token:{symbol}"),
                symbol=symbol,
                decimals=TOKEN_DECIMALS.get(symbol, 18),
                price_usd=Decimal(f"{max(price, 0.0001):.8f}"),
            )

        pools: dict[str, PoolSnapshot] = {}
        for t0, t1, _ in BASE_PAIRS:
            price0 = self._prices[t0]
            price1 = self._prices[t1]
            base_price = price0 / price1  # сколько token1 за 1 token0
            catalog = STABLE_DEX_CATALOG if _is_stable_pair(t0, t1) else DEX_CATALOG
            for dex, kind, fee_bps in catalog:
                # каждый пул отклоняется от «истинной» цены.
                # Для стейбл-пар масштаб отклонений на порядок меньше (1–50 bps вместо 50–500).
                is_stable = _is_stable_pair(t0, t1)
                # масштаб отклонений стейблов: ~1–15 bps в спокойном рынке,
                # до ~60 bps в хвосте (окно, на котором и живёт peg-арбитраж)
                scale = spread * (0.25 if is_stable else 1.0)
                deviation = rng.gauss(0, scale)
                if rng.random() < tail_prob * (0.35 if is_stable else 1.0):
                    deviation *= rng.uniform(2.0, 4.0)  # окно арбитража / событие депега
                pool_price = base_price * (1 + deviation)
                address = _addr(f"pool:{dex}:{t0}:{t1}:{self.chain_id}")
                # Стейбл-пулы в реальности глубоко капитализированы (Curve 3pool ~$100M):
                # иначе проскальзывание на $20–50k съедает весь peg-спред в несколько bps.
                if _is_stable_pair(t0, t1):
                    # Curve/Aerodrome держат десятки-сотни миллионов на стейбл-парах
                    base_tvl = rng.uniform(80.0, 260.0) if "curve_3pool" in dex else (
                        rng.uniform(40.0, 120.0) if "curve_ng" in dex else rng.uniform(20.0, 90.0)
                    )
                elif kind == "v3":
                    # Uniswap/Pancake V3 на blue-chip парах держат десятки миллионов (mainnet-реализм)
                    base_tvl = rng.uniform(25.0, 160.0)
                elif kind == "v2":
                    base_tvl = rng.uniform(8.0, 45.0)
                else:
                    base_tvl = rng.uniform(3.0, 20.0)
                tvl = Decimal(str(round(base_tvl * 1_000_000, 2)))
                pools[address] = _build_pool(
                    address=address,
                    dex=dex,
                    kind=kind,  # type: ignore[arg-type]
                    chain_id=self.chain_id,
                    token0=tokens[t0].address,
                    token1=tokens[t1].address,
                    price_token1_per_token0=pool_price,
                    decimals0=TOKEN_DECIMALS.get(t0, 18),
                    decimals1=TOKEN_DECIMALS.get(t1, 18),
                    fee_bps=fee_bps,
                    tvl_usd=tvl,
                    block_number=self._block,
                    rng=rng,
                    is_stable_pair=is_stable,
                )

        return MarketSnapshot(
            chain_id=self.chain_id,
            block_number=self._block,
            timestamp=time.time(),
            base_fee_gwei=round(8.0 + rng.uniform(0, 22), 3),
            native_price_usd=tokens["WETH"].price_usd,
            tokens={info.address.lower(): info for info in tokens.values()},
            pools=pools,
            positions=self._positions(tokens, rng),
            pending_swaps=self._pending_swaps(tokens, pools, rng),
            intents=self._intents(tokens, rng),
        )

    # ------------------------------------------------------------------ #
    def _positions(self, tokens: dict[str, TokenInfo], rng: random.Random) -> list[BorrowerPosition]:
        """Позиции для стратегии ликвидаций: большинство здоровые, 1–3 около порога."""
        positions: list[BorrowerPosition] = []
        for i in range(12):
            near = i < 3
            hf = rng.uniform(0.94, 1.02) if near else rng.uniform(1.08, 2.4)
            debt_symbol = rng.choice(["USDC", "USDT", "DAI"])
            coll_symbol = "WETH" if rng.random() < 0.6 else "WBTC"
            debt_usd = rng.uniform(8_000, 320_000)
            coll_usd = debt_usd * hf * rng.uniform(1.0, 1.6)
            debt_price = float(tokens[debt_symbol].price_usd)
            coll_price = float(tokens[coll_symbol].price_usd)
            positions.append(
                BorrowerPosition(
                    protocol=rng.choice(["aave_v3", "compound_v3", "morpho_blue", "spark", "euler_v2"]),
                    chain_id=self.chain_id,
                    user=_addr(f"borrower:{i}:{self.chain_id}"),
                    collateral_token=tokens[coll_symbol].address,
                    debt_token=tokens[debt_symbol].address,
                    collateral_amount=int(coll_usd / coll_price * 10 ** TOKEN_DECIMALS[coll_symbol]),
                    debt_amount=int(debt_usd / debt_price * 10 ** TOKEN_DECIMALS[debt_symbol]),
                    health_factor=round(hf, 4),
                    liquidation_bonus_bps=rng.choice([500, 750, 1000]),
                    close_factor=0.5,
                )
            )
        return positions

    def _pending_swaps(
        self, tokens: dict[str, TokenInfo], pools: dict[str, PoolSnapshot], rng: random.Random
    ) -> list[PendingSwap]:
        """Крупные свопы из «мемпула» — кандидаты на JIT и backrun.

        Размер свопа подбирается ОТ ГЛУБИНЫ пула: целевой ценовой импакт 5–40 бп —
        именно такие свопы интересны MEV-ботам. Свопы на тонких пулах, которые
        физически не могут «поехать» на миллион долларов, не генерируются: иначе
        имитатор давал бы нереалистично прибыльные backrun-возможности.
        """
        swaps: list[PendingSwap] = []
        by_address = {info.address: info for info in tokens.values()}
        # Кандидаты — самые глубокие пулы (там крупные свопы физически возможны) плюс
        # глубокие V3-пулы на волатильных парах: именно там живут JIT и backrun,
        # тогда как топ по TVL занимают стейбл-пулы с комиссией 1 бп.
        deep = sorted(pools.values(), key=lambda item: float(item.tvl_usd), reverse=True)[:6]
        volatile_v3 = sorted(
            (p for p in pools.values() if p.kind == "v3" and p.fee_bps >= 5),
            key=lambda item: float(item.tvl_usd),
            reverse=True,
        )[:3]
        candidates = deep + [p for p in volatile_v3 if p not in deep]
        for i in range(3):
            if not candidates:
                break
            pool = rng.choice(candidates)
            zero_for_one = rng.random() < 0.5
            token_in = pool.token0 if zero_for_one else pool.token1
            token_out = pool.token1 if zero_for_one else pool.token0
            info = by_address[token_in]
            # множитель «эффективной глубины»: V3 концентрированнее V2, у Curve (stableswap)
            # проскальзывание у паритета на порядок ниже, чем у constant-product
            depth_multiplier = {"v2": 1.0, "v3": 1.5, "stableswap": 2.5}.get(pool.kind, 1.0)
            impact = rng.uniform(0.0008, 0.0025)
            reserve_in = pool.reserve0 if zero_for_one else pool.reserve1
            amount = int(reserve_in * impact * depth_multiplier)
            if amount <= 0:
                continue
            size_usd = Decimal(str(round(amount / 10 ** info.decimals, 6))) * info.price_usd
            size_usd = Decimal(str(round(float(size_usd), 2)))
            swaps.append(
                PendingSwap(
                    tx_hash="0x" + hashlib.sha1(f"swap:{self._block}:{i}".encode()).hexdigest(),
                    chain_id=self.chain_id,
                    pool_address=pool.address,
                    dex=pool.dex,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=max(amount, 1),
                    size_usd=size_usd,
                    gas_price_gwei=round(rng.uniform(9, 40), 2),
                    to=_addr("router:universal"),
                    selector="0x3593564c",  # Universal Router: execute(bytes,bytes[],uint256)
                )
            )
        return swaps

    def _intents(self, tokens: dict[str, TokenInfo], rng: random.Random) -> list[CrossChainIntent]:
        """Кросс-чейн интенты с расхождением цены между сетями."""
        out: list[CrossChainIntent] = []
        for i, (bridge, dst, fee) in enumerate(
            [("across", 8453, 12), ("debridge_dln", 42161, 18), ("layerzero_oft", 10, 25)]
        ):
            token_symbol = "USDC" if i % 2 == 0 else "WETH"
            info = tokens[token_symbol]
            amount = int(rng.uniform(20_000, 400_000) / float(info.price_usd) * 10 ** info.decimals)
            out.append(
                CrossChainIntent(
                    bridge=bridge,
                    src_chain=self.chain_id,
                    dst_chain=dst,
                    token_src=info.address,
                    token_dst=info.address,
                    amount_in=max(amount, 1),
                    fee_bps=fee,
                    eta_sec=rng.randint(120, 900),
                    dst_price_usd=info.price_usd * Decimal(f"{1 + rng.gauss(0, 0.0025):.6f}"),
                )
            )
        return out


def _symbol_of(tokens: dict[str, TokenInfo], address: str) -> str:
    """Обратный поиск symbol по адресу токена."""
    for info in tokens.values():
        if info.address == address:
            return info.symbol
    return "UNKNOWN"


def _is_stable_pair(a: str, b: str) -> bool:
    """True, если пара стейблкоин-стейблкоин (для stableswap-пулов)."""
    stable = {"USDC", "USDT", "DAI"}
    return a in stable and b in stable


def _build_pool(
    *,
    address: str,
    dex: str,
    kind: str,
    chain_id: int,
    token0: str,
    token1: str,
    price_token1_per_token0: float,
    decimals0: int,
    decimals1: int,
    fee_bps: int,
    tvl_usd: Decimal,
    block_number: int,
    rng: random.Random,
    is_stable_pair: bool = False,
) -> PoolSnapshot:
    """Собирает состояние пула, согласованное с целевой ценой и TVL."""
    scale0 = 10**decimals0
    scale1 = 10**decimals1
    # reserves: token0_amount (в единицах) * price_token0_usd ~ TVL/2
    price_token0_usd = price_token1_per_token0 * _reference_price(token1)
    amount0 = float(tvl_usd) / 2 / max(price_token0_usd, 1e-9)
    amount1 = amount0 * price_token1_per_token0
    reserve0 = int(amount0 * scale0)
    reserve1 = int(amount1 * scale1)

    if kind == "v3":
        # On-chain sqrtPriceX96 кодирует RAW-отношение (с учётом decimals):
        #   price_raw = price_human * 10^(dec1 - dec0)
        price_raw = price_token1_per_token0 * (10**decimals1) / (10**decimals0)
        sqrt_price = int((price_raw**0.5) * Q96)
        # L для V3 = sqrt(x_raw * y_raw) / concentration: геометрическое среднее резервов
        # (эквивалент полного диапазона), делённое на коэффициент концентрации ликвидности.
        # Внимание: НЕЛЬЗЯ считать L через TVL/price без sqrt — иначе пул получается
        # на порядки тоньше настоящего и «создаёт» несуществующие арбитражи.
        depth = V3_STABLE_DEPTH_MULTIPLIER if is_stable_pair else V3_DEPTH_MULTIPLIER
        liquidity = int((reserve0 * reserve1) ** 0.5 * depth)
        tick_lower = -887200
        tick_upper = 887200
    else:
        sqrt_price = 0
        liquidity = 0
        tick_lower = tick_upper = 0

    return PoolSnapshot(
        address=address,
        dex=dex,
        kind=kind,  # type: ignore[arg-type]
        chain_id=chain_id,
        token0=token0,
        token1=token1,
        fee_bps=fee_bps,
        reserve0=reserve0,
        reserve1=reserve1,
        sqrt_price_x96=sqrt_price,
        liquidity=liquidity,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amp=100_000 if kind == "stableswap" else 0,  # A=1000 (в формате A*100)
        tvl_usd=tvl_usd,
        block_number=block_number,
    )


def _reference_price(symbol: str) -> float:
    """Референсные цены для масштабирования резервов (в USD)."""
    return {"WETH": 2650.0, "WBTC": 61000.0, "USDC": 1.0, "USDT": 1.0, "DAI": 1.0}.get(symbol, 1.0)


