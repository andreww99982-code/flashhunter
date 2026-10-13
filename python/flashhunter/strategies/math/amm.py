"""Математика AMM: Uniswap V2/V3, Curve stableswap, оптимизация размера сделки.

Все функции работают в целочисленной арифметике (как в EVM) — это критично:
результат должен совпадать с on-chain расчётом, иначе симуляция «увидит» прибыль,
которой нет. Единицы: amount_in / reserves — в минимальных единицах токена,
цены V3 — в формате sqrtPriceX96.

Ключевые формулы:
    V2 out:        dy = dx*(1-f)*y / (x + dx*(1-f))
    V3 out:        sqrtQ = L*sqrtP*Q96 / (L*Q96 + dx_eff*sqrtP)   (zeroForOne)
                   amount = L*(sqrtP - sqrtQ)/Q96
    Cycle N-hop:   profit(dx) вогнута на V2 (см. optimal_size_two_pool) — экстремум
                   ищется аналитически (2 хопа) либо тернарным поиском (N хопов).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

Q96 = 1 << 96
Q192 = 1 << 192
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

BPS = 10_000


# ===========================================================================
# Uniswap V2
# ===========================================================================
@dataclass(frozen=True, slots=True)
class PoolV2:
    """Постоянный продукт: x*y=k. reserve_in/reserve_out — для направления свопа."""

    reserve_in: int
    reserve_out: int
    fee_bps: int = 30  # 0.30%

    def amount_out(self, amount_in: int) -> int:
        """Выход свопа с учётом комиссии (целочисленно, как в контракте)."""
        if amount_in <= 0 or self.reserve_in <= 0 or self.reserve_out <= 0:
            return 0
        amount_in_with_fee = amount_in * (BPS - self.fee_bps)
        numerator = amount_in_with_fee * self.reserve_out
        denominator = self.reserve_in * BPS + amount_in_with_fee
        return numerator // denominator

    def amount_in_for_out(self, amount_out: int) -> int:
        """Сколько нужно подать на вход, чтобы получить ровно amount_out (±1 wei округления)."""
        if amount_out <= 0 or amount_out >= self.reserve_out:
            return 0
        numerator = self.reserve_in * amount_out * BPS
        denominator = (self.reserve_out - amount_out) * (BPS - self.fee_bps)
        return numerator // denominator + 1

    def price_impact_bps(self, amount_in: int) -> int:
        """Ценовое воздействие свопа в bps (спот-цена против исполненной)."""
        if amount_in <= 0 or self.reserve_in <= 0:
            return 0
        out = self.amount_out(amount_in)
        if out <= 0:
            return BPS
        spot = self.reserve_out / self.reserve_in
        executed = out / amount_in
        return int((1 - executed / spot) * BPS)


def optimal_size_two_pool(
    pool_ab: PoolV2, pool_ba: PoolV2, *, max_size: int | None = None
) -> int:
    """Аналитический оптимум размера dx для цикла A->B (pool_ab) -> A (pool_ba).

    Вывод. Пусть u = dx*k1 (вход после комиссии первого пула), k = (1-f)/10000.
    Тогда из первого пула: dy = u*y1/(x1+u); после второго пула с комиссией k2:
        out(u) = P*u / (Q + R*u),  P = y1*k2*y2, Q = x1*x2, R = x2 + y1*k2
        profit(u) = out(u) - u/k1
        d/du = P*Q/(Q+R*u)^2 - 1/k1 = 0  =>  u* = (sqrt(P*Q*k1) - Q) / R

    Расчёт ведётся в Decimal(50) — float64 теряет точность при малых спредах
    (катастрофическая отмена при sqrt(PQk1) ~ Q), а оптимум должен быть точным.

    Возвращает 0, если арбитража нет или оптимум неположителен.
    """
    k1 = Decimal(BPS - pool_ab.fee_bps) / BPS
    k2 = Decimal(BPS - pool_ba.fee_bps) / BPS
    x1, y1 = pool_ab.reserve_in, pool_ab.reserve_out
    x2, y2 = pool_ba.reserve_in, pool_ba.reserve_out
    if min(x1, y1, x2, y2) <= 0:
        return 0

    with localcontext() as ctx:
        ctx.prec = 50
        p_const = Decimal(y1) * k2 * Decimal(y2)
        q_const = Decimal(x1) * Decimal(x2)
        r_const = Decimal(x2) + Decimal(y1) * k2
        discriminant = p_const * q_const * k1
        if discriminant <= 0:
            return 0
        u = (discriminant.sqrt() - q_const) / r_const
        if u <= 0:
            return 0
        dx = int(u / k1)

    if max_size is not None:
        dx = min(dx, max_size)
    if dx <= 0:
        return 0
    profit = pool_ba.amount_out(pool_ab.amount_out(dx)) - dx
    return dx if profit > 0 else 0


def simulate_cycle_v2(pools: list[PoolV2], amount_in: int) -> int:
    """Прогоняет цепочку свопов через список V2-пулов; возвращает итоговый выход."""
    amount = amount_in
    for pool in pools:
        amount = pool.amount_out(amount)
        if amount <= 0:
            return 0
    return amount


# ===========================================================================
# Uniswap V3 (концентрированная ликвидность, один диапазон)
# ===========================================================================
@dataclass(frozen=True, slots=True)
class PoolV3:
    """Один диапазон концентрированной ликвидности.

    sqrt_price_x96 — текущая цена (Q64.96), liquidity — L активной позиции.
    tick_lower/tick_upper задают границы и нужны только для оценки частичного
    заполнения (когда расчётный объём выходит за диапазон).
    """

    sqrt_price_x96: int
    liquidity: int
    fee_bps: int = 5  # 0.05% (типовой пул USDC/WETH)
    tick_lower: int = -887272
    tick_upper: int = 887272

    def amount_out(self, amount_in: int, *, zero_for_one: bool = True) -> int:
        """Выход свопа в одном диапазоне (без пересечения тиков)."""
        if amount_in <= 0 or self.liquidity <= 0 or self.sqrt_price_x96 <= 0:
            return 0
        amount_after_fee = amount_in * (BPS - self.fee_bps) // BPS
        if amount_after_fee <= 0:
            return 0
        if zero_for_one:
            # price decreases: sqrtQ_x = L*sqrtP_x*Q96 / (L*Q96 + dx*sqrtP_x)
            numerator = self.liquidity * self.sqrt_price_x96 * Q96
            denominator = self.liquidity * Q96 + amount_after_fee * self.sqrt_price_x96
            sqrt_next = numerator // denominator
            if sqrt_next <= MIN_SQRT_RATIO:
                return 0
            return (self.liquidity * (self.sqrt_price_x96 - sqrt_next)) // Q96
        # one for zero: цена растёт, sqrtQ = sqrtP + dx*Q96/L.
        # ВНИМАНИЕ: выход измеряется в token0, поэтому формула отличается от zeroForOne:
        #   amount0 = L*(sqrtQ - sqrtP)*Q96 / (sqrtQ*sqrtP)
        sqrt_next = self.sqrt_price_x96 + (amount_after_fee * Q96) // self.liquidity
        if sqrt_next >= MAX_SQRT_RATIO:
            return 0
        return (self.liquidity * (sqrt_next - self.sqrt_price_x96) * Q96) // (
            sqrt_next * self.sqrt_price_x96
        )

    def amount_in_for_out(self, amount_out: int, *, zero_for_one: bool = True) -> int:
        """Требуемый вход (грубая оценка, пригодная для планирования размера)."""
        if amount_out <= 0 or self.liquidity <= 0:
            return 0
        if zero_for_one:
            if amount_out >= (self.liquidity * self.sqrt_price_x96) // Q96:
                return 0
            sqrt_next = self.sqrt_price_x96 - (amount_out * Q96) // self.liquidity
            numerator = self.liquidity * Q96 * (self.sqrt_price_x96 - sqrt_next)
            denominator = sqrt_next * self.sqrt_price_x96
        else:
            # Решаем относительно sqrtQ: amount0 = L*Q96*(sqrtQ-sqrtP)/(sqrtQ*sqrtP)
            #   => sqrtQ = L*Q96*sqrtP / (L*Q96 - amount0*sqrtP)
            denominator = self.liquidity * Q96 - amount_out * self.sqrt_price_x96
            if denominator <= 0:
                return 0
            sqrt_next = (self.liquidity * Q96 * self.sqrt_price_x96) // denominator
            # вход измеряется в token1: amount1 = L*Δsqrt_x / Q96
            numerator = self.liquidity * (sqrt_next - self.sqrt_price_x96)
            denominator = Q96
        if denominator == 0:
            return 0
        amount_after_fee = numerator // denominator
        return (amount_after_fee * BPS) // (BPS - self.fee_bps) + 1

    def marginal_price_ab(self) -> float:
        """Спот-цена token1/token0, приведённая к float (для сравнения пулов)."""
        price = (self.sqrt_price_x96 / Q96) ** 2
        return float(price)


def tick_to_sqrt_price_x96(tick: int) -> int:
    """sqrt(1.0001^tick) * 2^96 — реализация через экспоненту (для расчётов вне EVM)."""
    price = Decimal("1.0001") ** tick
    return int(price.sqrt() * Q96)


def v3_liquidity_for_amounts(
    sqrt_price_x96: int, sqrt_a_x96: int, sqrt_b_x96: int, amount0: int, amount1: int
) -> int:
    """L из пары (amount0, amount1) в диапазоне [a,b] — стандартные формулы Uniswap V3."""
    sqrt_price = Decimal(sqrt_price_x96) / Q96
    sqrt_a = Decimal(sqrt_a_x96) / Q96
    sqrt_b = Decimal(sqrt_b_x96) / Q96
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    liq0 = liq1 = Decimal(0)
    if sqrt_price <= sqrt_a:
        liq0 = Decimal(amount0) * sqrt_a * sqrt_b / (sqrt_b - sqrt_a)
    elif sqrt_price >= sqrt_b:
        liq1 = Decimal(amount1) / (sqrt_b - sqrt_a)
    else:
        liq0 = Decimal(amount0) * sqrt_price * sqrt_b / (sqrt_b - sqrt_price)
        liq1 = Decimal(amount1) / (sqrt_price - sqrt_a)
    liquidity = min(liq0, liq1)
    return int(liquidity)


# ===========================================================================
# Curve stableswap (2 монеты) — инвариант D, решаемый методом Ньютона
# ===========================================================================
STABLESWAP_A_PRECISION = 100
STABLESWAP_RATE_MULTIPLIER = 10**18


def _stableswap_d(xp: list[int], amp: int) -> int:
    """Решает инвариант: A*n^n*sum(x) + D = A*D*n^n + D^(n+1)/(n^n*prod(x))."""
    n = len(xp)
    sum_x = sum(xp)
    if sum_x == 0:
        return 0
    d = sum_x
    ann = amp * n
    for _ in range(255):
        d_prod = d
        for x in xp:
            d_prod = d_prod * d // (x * n)
        d_prev = d
        numerator = (ann * sum_x + d_prod * n) * d
        denominator = (ann - 1) * d + (n + 1) * d_prod
        d = numerator // denominator
        if abs(d - d_prev) <= 1:
            return d
    return d


def _stableswap_y(i: int, j: int, x: int, xp: list[int], amp: int) -> int:
    """Решает инвариант относительно y (стандартная процедура Curve get_y).

    Арифметика один-в-один повторяет Vyper-реализацию: S_ собирается по всем
    монетам кроме j, произведение — по всем кроме j (y заменяет j-ю монету).
    """
    n = len(xp)
    d = _stableswap_d(xp, amp)
    ann = amp * n
    c = d
    s = 0
    for k in range(n):
        if k == j:
            continue
        x_k = x if k == i else xp[k]
        s += x_k
        c = c * d // (x_k * n)
    c = c * d // (ann * n)
    b = s + d // ann
    y = d
    for _ in range(255):
        y_prev = y
        y = (y * y + c) // (2 * y + b - d)
        if abs(y - y_prev) <= 1:
            return y
    return y


def stableswap_get_dy(
    balances: list[int], i: int, j: int, dx: int, amp: int, fee_bps: int = 4
) -> int:
    """Обмен в Curve StableSwap: dx (с комиссией) -> dy с учётом инварианта D."""
    xp = balances.copy()
    x_new = xp[i] + dx * (BPS - fee_bps) // BPS
    y_new = _stableswap_y(i, j, x_new, xp, amp)
    dy = xp[j] - y_new - 1
    return dy * (BPS - fee_bps) // BPS


# ===========================================================================
# Численная оптимизация для N-хоповых циклов
# ===========================================================================
def ternary_search_optimal_size(
    profit_fn: "callable[[int], float]",  # type: ignore[valid-type]
    low: int,
    high: int,
    iterations: int = 48,
) -> int:
    """Тернарный поиск максимума прибыли для унимодальной profit_fn (V2/V3 цепочки).

    Для цепочек из AMM функция прибыли вогнута на диапазоне до «истощения» пулов,
    поэтому тернарный поиск сходится к глобальному максимуму за O(log(range)).
    """
    if low >= high:
        return low
    lo, hi = low, high
    for _ in range(iterations):
        if hi - lo <= 2:
            break
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if profit_fn(m1) < profit_fn(m2):
            lo = m1
        else:
            hi = m2
    candidates = range(lo, min(hi + 1, lo + 4))
    return max(candidates, key=profit_fn)


def gradient_ascent_optimal_size(
    profit_fn: "callable[[float], float]",  # type: ignore[valid-type]
    start: float,
    *,
    max_iterations: int = 32,
    tolerance: float = 1e-9,
    step: float | None = None,
    lower: float = 1.0,
    upper: float | None = None,
) -> float:
    """Градиентный подъём (центральная разность) — используется для нелинейных циклов."""
    x = start
    current = profit_fn(x)
    for _ in range(max_iterations):
        h = max(abs(x) * 1e-5, 1.0)
        deriv = (profit_fn(x + h) - profit_fn(x - h)) / (2 * h)
        if step is None:
            s = max(abs(x) * 0.05, 1.0)
        else:
            s = step
        candidate = x + s * deriv / (abs(deriv) + 1e-18) if deriv != 0 else x
        candidate = max(lower, candidate)
        if upper is not None:
            candidate = min(upper, candidate)
        value = profit_fn(candidate)
        if value <= current + tolerance:
            # второй шанс с половинным шагом — дотягиваем до оптимума
            candidate = max(lower, x + (candidate - x) / 2)
            value = profit_fn(candidate)
            if value <= current + tolerance:
                break
        x, current = candidate, value
    return x


def bps_to_pct(bps: float) -> float:
    """bps -> проценты."""
    return bps / 100.0


def pct_to_bps(pct: float) -> float:
    """проценты -> bps."""
    return pct * 100.0
