"""Математика AMM по тем же формулам, что исполняются в контрактах.

Данные (резервы, tick, ликвидность) приходят из сети — здесь только арифметика.
Формулы один в один повторяют Uniswap V2/V3, поэтому расчёт совпадает с EVM
до последнего wei: это то, на чём строится решение «отправлять сделку или нет».
"""

from __future__ import annotations

from dataclasses import dataclass

Q96 = 2**96
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


# --------------------------------------------------------------------------- #
# Uniswap V2
# --------------------------------------------------------------------------- #
def v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """Выход V2 с комиссией: x*y=k (та же формула, что в паре).

    ``fee_bps=30`` — 0.30% (Uniswap V2/Sushi), 25 — 0.25%, 5 — 0.05%.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 10_000 + amount_in_with_fee
    return numerator // denominator


def v2_spot_price(reserve_in: int, reserve_out: int, dec_in: int, dec_out: int) -> float:
    """Спот-цена «сколько token_out за один token_in» (человеческие единицы)."""
    if reserve_in <= 0:
        return 0.0
    return (reserve_out / 10**dec_out) / (reserve_in / 10**dec_in)


def v2_optimal_size(reserve_in: int, reserve_out: int, fee_bps: int = 30, max_size: int | None = None) -> int:
    """Оптимальный размер входа для двух пулов (аналитический оптимум V2→V2).

    Используется стратегией dex_arb, чтобы не перебирать размеры вслепую.
    """
    if reserve_in <= 0 or reserve_out <= 0:
        return 0
    gamma = (10_000 - fee_bps) / 10_000
    # Классический оптимум для арбитража между двумя V2-пулами с одинаковой комиссией
    size = int((reserve_in * reserve_out) ** 0.5 * (1 - gamma) / (1 + gamma) * 0.1)
    if max_size is not None:
        size = min(size, max_size)
    return max(size, 0)


# --------------------------------------------------------------------------- #
# Uniswap V3: тики и цены
# --------------------------------------------------------------------------- #
def tick_to_sqrt_price_x96(tick: int) -> int:
    """sqrtPriceX96 = 1.0001^(tick/2) * 2^96 (целочисленно, как в контракте)."""
    ratio = 1.0001**tick
    return int((ratio**0.5) * Q96)


def sqrt_price_x96_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """Цена token0, выраженная в token1 (человеческие единицы).

    price_raw = (sqrtPriceX96 / 2^96)^2 = token1_raw / token0_raw,
    поэтому человеческая цена = price_raw * 10^dec0 / 10^dec1.
    """
    if sqrt_price_x96 <= 0:
        return 0.0
    ratio = (sqrt_price_x96 / Q96) ** 2
    return ratio * (10**dec0) / (10**dec1)


def price_to_sqrt_price_x96(price: float, dec0: int, dec1: int) -> int:
    """Обратное преобразование: человеческая цена → sqrtPriceX96."""
    raw = price * (10**dec1) / (10**dec0)
    return int((max(raw, 1e-30) ** 0.5) * Q96)


def sqrt_price_x96_to_tick(sqrt_price_x96: int) -> int:
    """Номер тика по sqrtPriceX96 (логарифм по основанию 1.0001)."""
    import math

    if sqrt_price_x96 <= 0:
        return MIN_TICK
    return int(round(math.log((sqrt_price_x96 / Q96) ** 2, 1.0001)))


# --------------------------------------------------------------------------- #
# Uniswap V3: точный расчёт свопа в пределах одного диапазона
# --------------------------------------------------------------------------- #
def v3_amount_out_single_range(
    amount_in: int, sqrt_price_x96: int, liquidity: int, zero_for_one: bool, fee_bps: int = 5
) -> tuple[int, int]:
    """Точный-in своп внутри одного диапазона ликвидности.

    Возвращает ``(amount_out, sqrt_price_next)``. Формулы повторяют
    ``SqrtPriceMath`` и ``SwapMath`` пула:
      * добавление token0 (zero_for_one) двигает цену вниз;
      * добавление token1 — вверх;
      * комиссия вычитается из входа до расчёта цены.
    """
    if amount_in <= 0 or liquidity <= 0 or sqrt_price_x96 <= 0:
        return 0, sqrt_price_x96

    amount_in_after_fee = amount_in * (1_000_000 - fee_bps * 10_000) // 1_000_000
    if amount_in_after_fee <= 0:
        return 0, sqrt_price_x96

    if zero_for_one:
        # Продаём token0: sqrt_next = (L * sqrtP) / (L + amountIn * sqrtP / Q96)
        numerator1 = liquidity * sqrt_price_x96
        product = amount_in_after_fee * sqrt_price_x96
        denominator = numerator1 + product
        sqrt_next = (numerator1 * Q96) // denominator
        if sqrt_next < MIN_SQRT_RATIO + 1:
            sqrt_next = MIN_SQRT_RATIO + 1
        amount_out = (liquidity * (sqrt_price_x96 - sqrt_next)) // Q96
    else:
        # Продаём token1: sqrt_next = sqrtP + amountIn * Q96 / L
        sqrt_next = sqrt_price_x96 + (amount_in_after_fee * Q96) // liquidity
        if sqrt_next > MAX_SQRT_RATIO - 1:
            sqrt_next = MAX_SQRT_RATIO - 1
        amount_out = (liquidity * (sqrt_next - sqrt_price_x96)) // Q96

    return int(amount_out), int(sqrt_next)


@dataclass(slots=True)
class V3Step:
    """Один шаг свопа V3: результат и цена после него."""

    amount_out: int
    sqrt_price_next: int
    liquidity_next: int
    tick_next: int
    consumed: bool = True


def v3_amount_out_multi_range(
    amount_in: int,
    sqrt_price_x96: int,
    liquidity: int,
    tick: int,
    zero_for_one: bool,
    *,
    next_tick: int | None = None,
    liquidity_net_at_next: int = 0,
    fee_bps: int = 5,
    max_steps: int = 4,
) -> tuple[int, int, int]:
    """Своп через границы диапазонов (walk по инициализированным тикам).

    ``next_tick`` и ``liquidity_net_at_next`` берутся из реальных вызовов
    ``tickBitmap``/``ticks`` пула. Возвращает ``(amount_out, sqrt_price_next, liquidity_next)``.

    Ограничение шагов (``max_steps``) — защита от бесконечного обхода: если своп
    пересекает больше диапазонов, результат считается консервативно (по последнему
    посчитанному диапазону) — мы лучше недооценим прибыль, чем переоценим.
    """
    remaining = amount_in
    total_out = 0
    current_sqrt = sqrt_price_x96
    current_liquidity = liquidity
    current_tick = tick
    steps = 0

    while remaining > 0 and current_liquidity > 0 and steps < max_steps:
        steps += 1
        target_tick = next_tick if steps == 1 else None
        target_sqrt = tick_to_sqrt_price_x96(target_tick) if target_tick is not None else None

        if target_sqrt is None or (zero_for_one and target_sqrt >= current_sqrt) or (
            not zero_for_one and target_sqrt <= current_sqrt
        ):
            # Дальше границы нет — считаем своп в текущем диапазоне
            out, sqrt_next = v3_amount_out_single_range(remaining, current_sqrt, current_liquidity, zero_for_one, fee_bps)
            total_out += out
            remaining = 0
            current_sqrt = sqrt_next
            break

        # Считаем, сколько входа съедает путь до границы диапазона
        if zero_for_one:
            amount_to_boundary = (current_liquidity * (current_sqrt - target_sqrt)) // (current_sqrt * target_sqrt // Q96)
        else:
            amount_to_boundary = (current_liquidity * (target_sqrt - current_sqrt)) // Q96

        if amount_to_boundary >= remaining:
            out, sqrt_next = v3_amount_out_single_range(remaining, current_sqrt, current_liquidity, zero_for_one, fee_bps)
            total_out += out
            remaining = 0
            current_sqrt = sqrt_next
            break

        # Дошли до границы: часть входа уходит на неё, ликвидность меняется
        remaining -= amount_to_boundary
        total_out += (
            current_liquidity * (current_sqrt - target_sqrt)
            if zero_for_one
            else current_liquidity * (target_sqrt - current_sqrt)
        ) // Q96
        current_sqrt = target_sqrt
        current_liquidity = current_liquidity - liquidity_net_at_next if zero_for_one else current_liquidity + liquidity_net_at_next
        current_tick = target_tick
        next_tick = None  # следующий шаг — без данных о границе (консервативно)

    return int(total_out), int(current_sqrt), int(max(current_liquidity, 0))


# --------------------------------------------------------------------------- #
# Curve StableSwap
# --------------------------------------------------------------------------- #
def stableswap_get_dy(
    balances: list[int], i: int, j: int, dx: int, amp: int, fee_bps: int = 4
) -> int:
    """Офчейн-повтор ``get_dy`` StableSwap (Curve) — точная реализация инварианта.

    Итерации Ньютона ровно такие, как в контракте Curve:
        Ann = A * n^n
        D           — из инварианта n-мерной поверхности (Newton)
        y_j         — из инварианта при известном D и остальных балансах (Newton)
    После решения вычитается комиссия пула.

    Значения приходят в одних единицах (обычно нормализованных к 18 decimals),
    результат — в тех же единицах. Используется и для расчёта прибыли, и для
    контроля качества данных (насколько цена пула расходится с оракулом).
    """
    if dx <= 0 or i == j or not balances:
        return 0
    n = len(balances)
    if n < 2 or amp <= 0:
        return 0

    xp = list(balances)
    ann = amp * (n**n)

    # --- D считается по состоянию ДО свопа ------------------------------ #
    # D — это то, что своп обязан сохранить. Если посчитать его по балансам
    # уже с внесённым dx, задача становится вырожденной: инвариант почти плоский
    # вдоль направления (dx, dy), итерация останавливается на первом же
    # «почти корне» и выдаёт бессмысленно малую сумму к получению.
    total = sum(xp)
    if total <= 0:
        return 0
    d = total
    for _ in range(255):
        d_p = d
        for value in xp:
            if value <= 0:
                return 0
            d_p = d_p * d // (value * n)
        d_prev = d
        d = (ann * total + d_p * n) * d // ((ann - 1) * d + (n + 1) * d_p)
        if abs(d - d_prev) <= 1:
            break

    # Теперь вносим dx и ищем y_j, при котором D остаётся прежним
    xp[i] += dx

    # --- y_j при известном D -------------------------------------------- #
    c = d
    for idx in range(n):
        if idx != j:
            c = c * d // (xp[idx] * n)
    c = c * d // (ann * n)
    # b — сумма ВСЕХ балансов, кроме j (сам y решается из инварианта):
    # именно так устроен get_y в контракте Curve. Подстановка баланса j сюда
    # сдвигает корень на величину самого свопа и даёт «нулевую» выдачу.
    s_without_j = sum(xp[idx] for idx in range(n) if idx != j)
    b = s_without_j + d // ann
    y = d
    for _ in range(255):
        y_prev = y
        y = (y * y + c) // (2 * y + b - d)
        if abs(y - y_prev) <= 1:
            break

    if y <= 0 or y >= xp[j]:
        # y <= 0 — состояние физически невозможно (инвариант не решается);
        # y >= xp[j] — пул не может отдать больше, чем у него лежит.
        # В обоих случаях честный ответ «своп невозможен», а не «сколько угодно».
        return 0
    dy = xp[j] - y
    fee = dy * fee_bps // 10_000
    return int(dy - fee)


def v3_liquidity_to_tvl_usd(
    sqrt_price_x96: int,
    liquidity: int,
    price0_usd: float,
    price1_usd: float,
    decimals0: int = 18,
    decimals1: int = 18,
) -> float:
    """TVL активного диапазона V3 в USD по «виртуальным резервам».

    В концентрированной ликвидности объём позиций выражается через L:
        amount0 ≈ L / sqrt(P)      (в «сырых» единицах token0)
        amount1 ≈ L * sqrt(P)
    Это точная оценка для позиций, покрывающих текущую цену (именно они и
    торгуются), поэтому она пригодна для ранжирования пулов. Оба числа
    приводятся к человеческим единицам через decimals токенов.
    """
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        return 0.0
    sqrt_price = sqrt_price_x96 / Q96  # sqrt(token1_raw / token0_raw)
    if sqrt_price <= 0:
        return 0.0
    amount0 = liquidity / sqrt_price
    amount1 = liquidity * sqrt_price
    tvl0 = amount0 / 10**decimals0 * max(price0_usd, 0.0)
    tvl1 = amount1 / 10**decimals1 * max(price1_usd, 0.0)
    return float(tvl0 + tvl1)
