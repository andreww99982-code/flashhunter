"""Адаптеры: PoolSnapshot -> расчётные объекты AMM (единый интерфейс для стратегий).

Позволяет стратегиям не знать деталей реализации пулов: V2, V3 (концентрированная
ликвидность), Curve stableswap и Balancer weighted (приводится к V2 с поправкой
на вес, что для малых объёмов даёт ошибку < 0.05%).
"""

from __future__ import annotations

from decimal import Decimal

from flashhunter.market.snapshot import MarketSnapshot, PoolSnapshot
from flashhunter.strategies.math.amm import PoolV2, PoolV3, stableswap_get_dy

WEIGHTED_SLIPPAGE_NOTE = "weighted pools use V2 approximation for multi-hop pricing"


def is_zero_for_one(pool: PoolSnapshot, token_in: str) -> bool:
    """Направление свопа для V3: True, если входной токен — token0."""
    return token_in.lower() == pool.token0.lower()


# Кэш расчётных представлений пулов: пересоздавать объекты на каждой итерации
# численной оптимизации дорого (десятки тысяч аллокаций на снимок). Ключ включает
# блок и направление — при смене блока кэш сбрасывается (см. register_decimals).
_POOL_CACHE: dict[tuple[str, int, str], PoolV2 | PoolV3] = {}


def clear_pool_cache() -> None:
    """Сбрасывает кэш представлений (вызывается на каждом новом снимке)."""
    _POOL_CACHE.clear()


def v2_pool(pool: PoolSnapshot, token_in: str) -> PoolV2:
    """V2-представление пула в направлении token_in -> token_out (с кэшем)."""
    key = (pool.address, pool.block_number, token_in.lower())
    cached = _POOL_CACHE.get(key)
    if isinstance(cached, PoolV2):
        return cached
    if token_in.lower() == pool.token0.lower():
        view = PoolV2(reserve_in=pool.reserve0, reserve_out=pool.reserve1, fee_bps=pool.fee_bps)
    else:
        view = PoolV2(reserve_in=pool.reserve1, reserve_out=pool.reserve0, fee_bps=pool.fee_bps)
    _POOL_CACHE[key] = view
    return view


def v3_pool(pool: PoolSnapshot) -> PoolV3:
    """V3-представление пула (с кэшем)."""
    key = (pool.address, pool.block_number, "v3")
    cached = _POOL_CACHE.get(key)
    if isinstance(cached, PoolV3):
        return cached
    view = PoolV3(
        sqrt_price_x96=pool.sqrt_price_x96,
        liquidity=pool.liquidity,
        fee_bps=pool.fee_bps,
        tick_lower=pool.tick_lower,
        tick_upper=pool.tick_upper,
    )
    _POOL_CACHE[key] = view
    return view


def pool_spot_price(pool: PoolSnapshot, token_in: str) -> float:
    """Спотовая цена пула: сколько token_out за 1 token_in в человеческих единицах.

    Используется как дешёвый отсев: если произведение цен по циклу не даёт запаса
    хотя бы на комиссии, нет смысла запускать численную оптимизацию размера.
    """
    try:
        if pool.kind == "v2":
            view = v2_pool(pool, token_in)
            if view.reserve_in <= 0 or view.reserve_out <= 0:
                return 0.0
            dec_in = _DECIMALS_CACHE.get((token_in.lower(), pool.chain_id), 18)
            out_token = pool.token1 if is_zero_for_one(pool, token_in) else pool.token0
            dec_out = _DECIMALS_CACHE.get((out_token.lower(), pool.chain_id), 18)
            ratio = (view.reserve_out / 10**dec_out) / (view.reserve_in / 10**dec_in)
            return ratio * (1 - pool.fee_bps / 10_000)
        if pool.kind == "v3":
            if pool.sqrt_price_x96 <= 0:
                return 0.0
            raw = (pool.sqrt_price_x96 / 2**96) ** 2  # token1_raw / token0_raw
            dec0 = _DECIMALS_CACHE.get((pool.token0.lower(), pool.chain_id), 18)
            dec1 = _DECIMALS_CACHE.get((pool.token1.lower(), pool.chain_id), 18)
            price_1_in_0 = raw * 10 ** (dec0 - dec1)          # token1 за 1 token0
            if is_zero_for_one(pool, token_in):
                return price_1_in_0 * (1 - pool.fee_bps / 10_000)
            return (1 / price_1_in_0 if price_1_in_0 > 0 else 0.0) * (1 - pool.fee_bps / 10_000)
        if pool.kind == "stableswap":
            price = _STABLE_SPOT.get(pool.address)
            if price is None:
                return 0.0
            dec0 = _DECIMALS_CACHE.get((pool.token0.lower(), pool.chain_id), 18)
            dec1 = _DECIMALS_CACHE.get((pool.token1.lower(), pool.chain_id), 18)
            # price хранится как «token1 за token0» в человеческих единицах
            return price if is_zero_for_one(pool, token_in) else (1 / price if price > 0 else 0.0)
    except (ZeroDivisionError, ValueError, OverflowError):
        return 0.0
    return 0.0


# Спотовая цена стейбл-пулов: заполняется тем, кто читает пул (market.live)
_STABLE_SPOT: dict[str, float] = {}


def register_stable_spot(address: str, price: float) -> None:
    """Сохраняет спотовую цену стейбл-пула (token1 за token0), посчитанную по инварианту."""
    if price > 0:
        _STABLE_SPOT[address] = price


NORM_DECIMALS = 18


def amount_out(pool: PoolSnapshot, token_in: str, amount_in: int) -> int:
    """Выход свопа любого поддерживаемого типа пула (целочисленно, как on-chain)."""
    if amount_in <= 0:
        return 0
    if pool.kind == "v3":
        return v3_pool(pool).amount_out(amount_in, zero_for_one=is_zero_for_one(pool, token_in))
    if pool.kind == "stableswap":
        # Curve сравнивает балансы в нормализованных единицах: balance * 10^(18-dec)
        dec0, dec1 = _decimals(pool)
        i, j = (0, 1) if is_zero_for_one(pool, token_in) else (1, 0)
        balances = [
            pool.reserve0 * 10 ** (NORM_DECIMALS - dec0),
            pool.reserve1 * 10 ** (NORM_DECIMALS - dec1),
        ]
        dx_norm = amount_in * 10 ** (NORM_DECIMALS - (dec0 if i == 0 else dec1))
        amp = pool.amp or 100_000
        dy_norm = stableswap_get_dy(balances, i, j, dx_norm, amp, pool.fee_bps)
        out_dec = dec1 if j == 1 else dec0
        return dy_norm // 10 ** (NORM_DECIMALS - out_dec)
    return v2_pool(pool, token_in).amount_out(amount_in)


def _decimals(pool: PoolSnapshot) -> tuple[int, int]:
    """Кэш decimals токенов пула (заполняется вызывающим кодом через ``_DECIMALS_CACHE``)."""
    return _DECIMALS_CACHE.get((pool.token0.lower(), pool.chain_id), 18), _DECIMALS_CACHE.get(
        (pool.token1.lower(), pool.chain_id), 18
    )


# Заполняется strategies.service при получении снимка (единая точка правды по decimals)
_DECIMALS_CACHE: dict[tuple[str, int], int] = {}


def register_decimals(snapshot: MarketSnapshot) -> None:
    """Обновляет кэш decimals из снимка и сбрасывает представления пулов.

    Вызывается на каждом блоке: state пулов изменился, поэтому кэш расчётных
    объектов больше не актуален.
    """
    for info in snapshot.tokens.values():
        _DECIMALS_CACHE[(info.address.lower(), snapshot.chain_id)] = info.decimals
    clear_pool_cache()


def spot_rate(pool: PoolSnapshot, snapshot: MarketSnapshot, token_in: str) -> Decimal:
    """Спот-курс token_out за token_in с учётом decimals (для сравнения пулов)."""
    token_out = pool.token1 if is_zero_for_one(pool, token_in) else pool.token0
    if pool.kind == "v3":
        sqrt_price = Decimal(pool.sqrt_price_x96) / Decimal(1 << 96)
        price_1_per_0 = sqrt_price * sqrt_price  # token1 за token0 (в raw-единицах)
        raw = price_1_per_0 if is_zero_for_one(pool, token_in) else 1 / price_1_per_0
    else:
        reserve_in = pool.reserve_of(token_in)
        reserve_out = pool.reserve_of(token_out)
        if reserve_in <= 0:
            return Decimal("0")
        raw = Decimal(reserve_out) / Decimal(reserve_in)
    in_info = snapshot.token(token_in)
    out_info = snapshot.token(token_out)
    human = raw * (Decimal(10) ** in_info.decimals) / (Decimal(10) ** out_info.decimals)
    return human


def probe_amount(pool: PoolSnapshot, snapshot: MarketSnapshot, token_in: str, usd: Decimal) -> int:
    """Размер пробы в минимальных единицах токена, эквивалентный usd."""
    info = snapshot.token(token_in)
    if info.price_usd <= 0:
        return 0
    return max(int(usd / info.price_usd * (10**info.decimals)), 1)


def apply_swap(pool: PoolSnapshot, token_in: str, amount_in: int) -> None:
    """Мутирует снимок пула, применяя своп (используется для локальной симуляции цепочек)."""
    token_out = pool.token1 if is_zero_for_one(pool, token_in) else pool.token0
    out = amount_out(pool, token_in, amount_in)
    if out <= 0:
        return
    if is_zero_for_one(pool, token_in):
        pool.reserve0 += amount_in
        pool.reserve1 = max(pool.reserve1 - out, 1)
    else:
        pool.reserve1 += amount_in
        pool.reserve0 = max(pool.reserve0 - out, 1)


def clone_pool(pool: PoolSnapshot) -> PoolSnapshot:
    """Глубокая копия снимка пула (для симуляции без искажения рабочего состояния)."""
    from dataclasses import replace

    return replace(pool)
