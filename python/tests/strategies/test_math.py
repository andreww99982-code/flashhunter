"""Тесты AMM-математики: V2, V3, stableswap и оптимизаторы размера."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from flashhunter.market.mock import (
    BASE_PAIRS,
    DEX_CATALOG,
    MockMarketSource,
    _addr,
    _build_pool,
)
from flashhunter.strategies.math.adapters import (
    amount_out,
    apply_swap,
    clone_pool,
    probe_amount,
    register_decimals,
    spot_rate,
)
from flashhunter.strategies.math.amm import (
    PoolV2,
    PoolV3,
    optimal_size_two_pool,
    simulate_cycle_v2,
    stableswap_get_dy,
    ternary_search_optimal_size,
)

BPS = 10_000


@pytest.fixture(scope="module")
def snapshot():
    """Снимок мок-рынка с зарегистрированными decimals (нужно для Curve)."""
    source = MockMarketSource(seed=5, profile="normal")
    snap = source.next_snapshot()
    register_decimals(snap)
    return snap


# --------------------------------------------------------------------------- #
# Uniswap V2
# --------------------------------------------------------------------------- #
def test_v2_amount_out_matches_formula() -> None:
    """Выход V2 считается по формуле x*y=k с комиссией."""
    pool = PoolV2(reserve_in=10**21, reserve_out=10**21, fee_bps=30)
    dx = 10**18
    amount_after_fee = dx * (BPS - 30)
    expected = amount_after_fee * 10**21 // (10**21 * BPS + amount_after_fee)
    assert pool.amount_out(dx) == expected
    assert 0 < pool.amount_out(dx) < dx  # паритет 1:1: комиссия + импакт


def test_v2_monotonic_and_bounded() -> None:
    """Выход растёт с размером входа и всегда меньше резерва."""
    pool = PoolV2(reserve_in=10**22, reserve_out=10**22, fee_bps=30)
    outputs = [pool.amount_out(10**18 * step) for step in range(1, 8)]
    assert outputs == sorted(outputs)
    assert pool.amount_out(10**22) < 10**22


def test_v2_amount_in_for_out_inverts() -> None:
    """amount_in_for_out — обратная функция к amount_out (±1 wei округления)."""
    pool = PoolV2(reserve_in=5 * 10**21, reserve_out=8 * 10**21, fee_bps=30)
    target = 10**18
    required = pool.amount_in_for_out(target)
    assert required > 0
    assert abs(pool.amount_out(required) - target) <= 2


def test_v2_price_impact_grows_with_size() -> None:
    """Слиппедж монотонно растёт с размером сделки."""
    pool = PoolV2(reserve_in=10**21, reserve_out=10**21, fee_bps=30)
    impacts = [pool.price_impact_bps(10**18 * step) for step in range(1, 6)]
    assert impacts == sorted(impacts)
    assert impacts[0] > 0


# --------------------------------------------------------------------------- #
# Uniswap V3
# --------------------------------------------------------------------------- #
def test_v3_matches_v2_when_full_range_no_fee() -> None:
    """V3 с L=sqrt(x*y) и полным диапазоном обязан совпасть с V2 без комиссии."""
    x, y = 10**21, 10**21
    liquidity = int((x * y) ** 0.5)
    v2 = PoolV2(reserve_in=x, reserve_out=y, fee_bps=0)
    v3 = PoolV3(liquidity=liquidity, sqrt_price_x96=int((y / x) ** 0.5 * (1 << 96)), fee_bps=0)
    for dx in (10**17, 10**18, 10**19, 10**20):
        assert v3.amount_out(dx, zero_for_one=True) == v2.amount_out(dx)


def test_v3_inverse_direction_is_consistent() -> None:
    """amount_in_for_out — обратная функция к amount_out для обоих направлений."""
    pool = PoolV3(liquidity=5 * 10**22, sqrt_price_x96=1 << 96, fee_bps=5)
    for zero_for_one in (True, False):
        dx = 3 * 10**18
        dy = pool.amount_out(dx, zero_for_one=zero_for_one)
        assert dy > 0
        dx_back = pool.amount_in_for_out(dy + 1, zero_for_one=zero_for_one)
        assert abs(dx_back - dx) <= max(10**15, dx // 10**4)


def test_v3_price_encodes_raw_price() -> None:
    """sqrtPriceX96 кодирует RAW-цену с учётом decimals (иначе V3-расчёты врут)."""
    rng = random.Random(3)
    pool = _build_pool(
        address=_addr("test:pool"),
        dex="uniswap_v3",
        kind="v3",
        chain_id=1,
        token0=_addr("token:WETH"),
        token1=_addr("token:USDC"),
        price_token1_per_token0=2650.0,
        decimals0=18,
        decimals1=6,
        fee_bps=5,
        tvl_usd=Decimal("100000000"),
        block_number=1,
        rng=rng,
        is_stable_pair=False,
    )
    raw_price = (pool.sqrt_price_x96 / (1 << 96)) ** 2
    assert abs(raw_price * 10 ** (18 - 6) - 2650.0) / 2650.0 < 0.01


# --------------------------------------------------------------------------- #
# Оптимизаторы и циклы
# --------------------------------------------------------------------------- #
def test_optimal_size_two_pool_is_local_maximum() -> None:
    """Аналитический оптимум размера — действительно максимум прибыли.

    Ориентация пулов в цикле X->Y->X: pool_ab (вход X, выход Y) и pool_ba (вход Y, выход X).
    """
    token_x, token_y = 10**22, 10**22
    dex_ab = PoolV2(reserve_in=token_x, reserve_out=token_y, fee_bps=30)  # Y по паритету
    dex_ba = PoolV2(reserve_in=token_y * 985 // 1000, reserve_out=token_x, fee_bps=30)  # Y дороже на 1.5%

    size = optimal_size_two_pool(dex_ab, dex_ba)
    assert size > 0

    def profit(dx: int) -> int:
        return dex_ba.amount_out(dex_ab.amount_out(dx)) - dx

    best = profit(size)
    assert best > 0
    assert best >= profit(size // 2)
    assert best >= profit(min(size * 2, 10**22))


def test_optimal_size_respects_max_size() -> None:
    """Верхняя граница размера не нарушается (лимит экспозиции)."""
    dex_ab = PoolV2(reserve_in=10**23, reserve_out=10**23, fee_bps=5)
    dex_ba = PoolV2(reserve_in=10**23 * 99 // 100, reserve_out=10**23, fee_bps=5)
    size = optimal_size_two_pool(dex_ab, dex_ba, max_size=10**19)
    assert 0 < size <= 10**19


def test_optimal_size_zero_without_opportunity() -> None:
    """Без расхождения цен арбитража нет (оптимум = 0)."""
    dex_ab = PoolV2(reserve_in=10**22, reserve_out=10**22, fee_bps=30)
    dex_ba = PoolV2(reserve_in=10**22, reserve_out=10**22, fee_bps=30)
    assert optimal_size_two_pool(dex_ab, dex_ba) == 0


def test_ternary_search_finds_peak() -> None:
    """Тернарный поиск находит максимум унимодальной функции."""
    peak = ternary_search_optimal_size(lambda dx: -(dx - 5_000) ** 2 + 1_000_000, 0, 10_000, iterations=60)
    assert abs(peak - 5_000) <= 5


def test_simulate_cycle_v2_positive_for_arbitrage() -> None:
    """Цикл через два пула с расхождением цен возвращает больше, чем вложено."""
    dex_ab = PoolV2(reserve_in=10**22, reserve_out=10**22, fee_bps=30)
    dex_ba = PoolV2(reserve_in=10**22 * 98 // 100, reserve_out=10**22, fee_bps=30)
    # размер берём из оптимизатора: на слишком крупной сделке комиссии съедают спред
    dx = optimal_size_two_pool(dex_ab, dex_ba)
    assert dx > 0
    cycle_out = simulate_cycle_v2([dex_ab, dex_ba], dx)
    assert cycle_out > dx
    assert simulate_cycle_v2([dex_ab, dex_ba], dx // 2) < cycle_out


# --------------------------------------------------------------------------- #
# Curve (stableswap)
# --------------------------------------------------------------------------- #
def test_stableswap_get_dy_monotonic_and_tight() -> None:
    """Curve-математика: выход растёт с входом, слиппедж у паритета минимален."""
    balances = [10**12, 10**12]
    small = stableswap_get_dy(balances, 0, 1, 10**9, amp=200, fee_bps=1)
    big = stableswap_get_dy(balances, 0, 1, 10**11, amp=200, fee_bps=1)
    assert 0 < small < big
    assert abs(big - 10**11) / 10**11 < 0.005  # < 50 бп слиппеджа на 10% пула


def test_stableswap_amplification_reduces_slippage() -> None:
    """Чем выше A, тем плотнее цена у паритета (свойство Curve)."""
    balances = [10**12, 10**12]
    dx = 10**11
    low_a = stableswap_get_dy(balances, 0, 1, dx, amp=10, fee_bps=0)
    high_a = stableswap_get_dy(balances, 0, 1, dx, amp=2000, fee_bps=0)
    assert high_a >= low_a


def test_stableswap_skewed_pool_penalizes_swap() -> None:
    """Сдвинутый от паритета пул отдаёт меньше (депег-риск)."""
    balanced = stableswap_get_dy([10**12, 10**12], 0, 1, 10**10, amp=200, fee_bps=4)
    skewed = stableswap_get_dy([10**12 * 12 // 10, 10**12 * 8 // 10], 0, 1, 10**10, amp=200, fee_bps=4)
    assert skewed < balanced


# --------------------------------------------------------------------------- #
# Адаптеры (мост «снимок рынка → AMM-математика»)
# --------------------------------------------------------------------------- #
def test_adapters_amount_out_and_apply_swap(snapshot) -> None:
    """Адаптеры считают выход и мутируют состояние; клон защищает оригинал."""
    v2_addresses = [address for address, pool in snapshot.pools.items() if pool.kind == "v2"]
    pool = snapshot.pools[v2_addresses[0]]
    token_in = pool.token0
    dx = probe_amount(pool, snapshot, token_in, Decimal("5000"))
    assert dx > 0

    rate_before = spot_rate(pool, snapshot, token_in)
    assert amount_out(pool, token_in, dx) > 0

    clone = clone_pool(pool)
    apply_swap(clone, token_in, dx)
    assert spot_rate(clone, snapshot, token_in) != rate_before
    assert spot_rate(pool, snapshot, token_in) == rate_before  # оригинал не тронут


def test_register_decimals_enables_stableswap_math(snapshot) -> None:
    """Без register_decimals Curve-расчёты были бы в неверном масштабе."""
    stable = [pool for pool in snapshot.pools.values() if pool.kind == "stableswap"]
    assert stable, "в мок-рынке должны быть стейбл-пулы"
    pool = stable[0]
    dx = 10 ** snapshot.token(pool.token0).decimals
    out = amount_out(pool, pool.token0, dx)
    assert out > 0
    human_in = Decimal(dx) / Decimal(10) ** snapshot.token(pool.token0).decimals
    human_out = Decimal(out) / Decimal(10) ** snapshot.token(pool.token1).decimals
    assert 0.985 < float(human_out / human_in) < 1.005


def test_probe_amount_uses_price_and_decimals(snapshot) -> None:
    """probe_amount переводит USD в минимальные единицы токена."""
    pool = next(iter(snapshot.pools.values()))
    token = pool.token0
    info = snapshot.token(token)
    raw = probe_amount(pool, snapshot, token, Decimal("10000"))
    human = Decimal(raw) / Decimal(10) ** info.decimals
    assert abs(float(human * info.price_usd) - 10_000) / 10_000 < 0.02


# --------------------------------------------------------------------------- #
# Мок-рынок (детерминизм и реализм)
# --------------------------------------------------------------------------- #
def test_mock_source_is_deterministic() -> None:
    """Одинаковый seed → одинаковый рынок (воспроизводимые бэктесты)."""
    first = MockMarketSource(seed=77, profile="volatile").next_snapshot()
    second = MockMarketSource(seed=77, profile="volatile").next_snapshot()
    assert first.block_number == second.block_number
    assert {k: str(v.tvl_usd) for k, v in first.pools.items()} == {
        k: str(v.tvl_usd) for k, v in second.pools.items()
    }
    assert [str(s.size_usd) for s in first.pending_swaps] == [str(s.size_usd) for s in second.pending_swaps]


def test_mock_pending_swaps_have_realistic_impact() -> None:
    """Крупные свопы из мемпула двигают цену в пределах, а не на десятки процентов."""
    source = MockMarketSource(seed=11, profile="volatile")
    checked = 0
    for _ in range(10):
        snap = source.next_snapshot()
        register_decimals(snap)
        for swap in snap.pending_swaps:
            pool = snap.pools[swap.pool_address]
            clone = clone_pool(pool)
            rate_before = spot_rate(clone, snap, swap.token_in)
            apply_swap(clone, swap.token_in, swap.amount_in)
            rate_after = spot_rate(clone, snap, swap.token_in)
            change_bps = abs(float(rate_after - rate_before) / float(rate_before)) * 10_000
            assert change_bps < 250, f"своп слишком крупный для пула: {change_bps:.1f} бп"
            checked += 1
    assert checked > 10


def test_base_pairs_and_catalogs_consistent() -> None:
    """Каталоги DEX покрывают базовые пары и содержат и V2, и V3."""
    kinds = {kind for _, kind, _ in DEX_CATALOG}
    assert {"v2", "v3"} <= kinds
    assert len(BASE_PAIRS) >= 3
    for _, _, price in BASE_PAIRS:
        assert price > 0
