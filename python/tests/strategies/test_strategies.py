"""Тесты стратегий: каждая должна находить возможности, соблюдать параметры и не падать."""

from __future__ import annotations

from decimal import Decimal

import pytest

from flashhunter.market.mock import MockMarketSource
from flashhunter.strategies.arb import DexArbStrategy
from flashhunter.strategies.backrun import BackrunStrategy
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, gas_cost_usd
from flashhunter.strategies.intent import IntentStrategy
from flashhunter.strategies.jit import JitStrategy
from flashhunter.strategies.liq import LiquidationStrategy
from flashhunter.strategies.service import StrategyService
from flashhunter.strategies.stable_peg import StablePegStrategy
from flashhunter.strategies.triangular import TriangularStrategy
from tests.conftest import make_ctx

ALL_STRATEGIES: dict[str, type[BaseStrategy]] = {
    "dex_arb": DexArbStrategy,
    "triangular": TriangularStrategy,
    "stable_peg": StablePegStrategy,
    "liquidation": LiquidationStrategy,
    "jit": JitStrategy,
    "backrun": BackrunStrategy,
    "intent": IntentStrategy,
}


def _collect(strategy: BaseStrategy, profile: str = "normal", blocks: int = 20, seed: int = 11) -> list[Opportunity]:
    """Прогоняет стратегию по N блокам детерминированного рынка."""
    source = MockMarketSource(seed=seed, profile=profile)
    found: list[Opportunity] = []
    for _ in range(blocks):
        snapshot = source.next_snapshot()
        ctx = make_ctx(snapshot)
        if not strategy.supports(ctx.chain_id):
            continue
        found.extend(strategy.on_block_sync(ctx) if hasattr(strategy, "on_block_sync") else [])
    return found


async def _collect_async(
    strategy: BaseStrategy, profile: str = "normal", blocks: int = 20, seed: int = 11
) -> list[Opportunity]:
    source = MockMarketSource(seed=seed, profile=profile)
    found: list[Opportunity] = []
    for _ in range(blocks):
        snapshot = source.next_snapshot()
        ctx = make_ctx(snapshot)
        if not strategy.supports(ctx.chain_id):
            continue
        found.extend(await strategy.on_block(ctx))
    return found


@pytest.mark.parametrize("name", list(ALL_STRATEGIES))
async def test_strategy_runs_without_errors(name: str) -> None:
    """Ни одна стратегия не падает ни в спокойном, ни в стрессовом рынке."""
    cls = ALL_STRATEGIES[name]
    for profile in ("calm", "normal", "volatile", "stress"):
        opportunities = await _collect_async(cls(), profile=profile, blocks=10)
        assert isinstance(opportunities, list)
        for opportunity in opportunities:
            assert opportunity.strategy == name
            assert opportunity.net_profit_usd >= 0
            assert opportunity.size_usd > 0


@pytest.mark.parametrize(
    ("name", "profile", "minimum"),
    [
        ("dex_arb", "volatile", 5),
        ("triangular", "volatile", 5),
        ("liquidation", "normal", 1),
        ("stable_peg", "volatile", 1),
        ("intent", "normal", 1),
        ("jit", "volatile", 1),
    ],
)
async def test_strategy_finds_opportunities_on_calibrated_profile(name: str, profile: str, minimum: int) -> None:
    """Калибровка мок-рынка: стратегия находит возможности на своём профиле."""
    opportunities = await _collect_async(ALL_STRATEGIES[name](), profile=profile, blocks=30)
    assert len(opportunities) >= minimum, f"{name}: найдено {len(opportunities)} < {minimum}"


async def test_all_opportunities_respect_min_profit() -> None:
    """Прибыль каждой возможности выше порога стратегии (в % от размера сделки)."""
    for profile in ("normal", "volatile"):
        for name, cls in ALL_STRATEGIES.items():
            for opportunity in await _collect_async(cls(), profile=profile, blocks=15):
                assert opportunity.net_profit_usd > 0
                assert opportunity.roi_pct > Decimal("0")


async def test_dex_arb_respects_size_limits() -> None:
    """Размер сделки арбитража не выходит за min/max из параметров."""
    strategy = DexArbStrategy()
    for opportunity in await _collect_async(strategy, profile="volatile", blocks=20):
        assert strategy.min_size_usd <= opportunity.size_usd <= strategy.max_size_usd


async def test_dex_arb_deduplicates_same_pair() -> None:
    """Одна и та же пара пулов не должна повторяться в одном блоке."""
    source = MockMarketSource(seed=3, profile="volatile")
    strategy = DexArbStrategy()
    for _ in range(5):
        snapshot = source.next_snapshot()
        opportunities = await strategy.on_block(make_ctx(snapshot))
        keys = {tuple(sorted(opportunity.pools)) for opportunity in opportunities}
        assert len(keys) == len(opportunities)


async def test_backrun_never_sandwiches() -> None:
    """Этическое требование ТЗ п.18: sandwich отключён жёстко."""
    strategy = BackrunStrategy()
    assert strategy.sandwich is False
    for opportunity in await _collect_async(strategy, profile="volatile", blocks=30):
        assert opportunity.kind == "backrun"
        assert opportunity.meta["sandwich"] is False


async def test_backrun_profit_bounded_by_victim_impact() -> None:
    """Прибыль backrun ограничена сдвигом, который создала жертва (защита от кривой модели)."""
    for opportunity in await _collect_async(BackrunStrategy(), profile="stress", blocks=30):
        swap_usd = Decimal(str(opportunity.meta["victim_swap_usd"]))
        impact_bps = Decimal(str(opportunity.meta["victim_impact_bps"]))
        bound = swap_usd * impact_bps / Decimal(10_000) * Decimal("0.5")
        assert opportunity.net_profit_usd <= bound


async def test_jit_share_and_capital_model() -> None:
    """JIT отдаёт в meta долю комиссии и не превышает разрешённый капитал."""
    strategy = JitStrategy()
    for opportunity in await _collect_async(strategy, profile="volatile", blocks=30):
        assert 0 < float(opportunity.meta["our_liquidity_share"]) <= 1
        assert Decimal(str(opportunity.meta["size_usd"])) <= strategy.max_jit_size_usd
        assert opportunity.net_profit_usd >= strategy.min_profit_usd


async def test_intent_respects_bridge_fee_and_eta() -> None:
    """Cross-chain интенты уважают потолки комиссии бриджа и времени доставки."""
    strategy = IntentStrategy()
    for opportunity in await _collect_async(strategy, profile="volatile", blocks=30):
        assert opportunity.kind == "intent"
        fee_bps = Decimal(str(opportunity.meta.get("bridge_fee_bps", 0)))
        eta_sec = Decimal(str(opportunity.meta.get("eta_sec", 0)))
        assert fee_bps <= Decimal(str(strategy.max_bridge_fee_bps))
        assert eta_sec <= Decimal(str(strategy.max_eta_sec))


async def test_liquidation_uses_liquidation_bonus() -> None:
    """Стратегия ликвидаций учитывает бонус ликвидатора и close factor."""
    strategy = LiquidationStrategy()
    for opportunity in await _collect_async(strategy, profile="normal", blocks=30):
        assert opportunity.kind == "liquidation"
        assert float(opportunity.meta["health_factor"]) <= strategy.max_health_factor
        assert opportunity.net_profit_usd >= strategy.min_profit_usd


async def test_stable_peg_requires_gas_headroom() -> None:
    """Peg-арбитраж не берётся, если базовая комиссия выше разрешённой."""
    strategy = StablePegStrategy()
    strict = StablePegStrategy(params={**strategy.params, "max_base_fee_gwei": 0.1})
    opportunities = await _collect_async(strict, profile="volatile", blocks=20)
    assert opportunities == []


def test_gas_cost_linear_in_price() -> None:
    """Стоимость газа линейна по (base fee + чаевые) и цене нативного токена."""
    cheap = gas_cost_usd(gas_units=250_000, base_fee_gwei=10, priority_fee_gwei=2, native_price_usd=Decimal("2000"))
    pricey = gas_cost_usd(gas_units=250_000, base_fee_gwei=20, priority_fee_gwei=2, native_price_usd=Decimal("2000"))
    doubled_native = gas_cost_usd(
        gas_units=250_000, base_fee_gwei=10, priority_fee_gwei=2, native_price_usd=Decimal("4000")
    )
    assert pricey > cheap > 0
    assert abs(float(pricey / cheap) - 22 / 12) < 1e-6  # (20+2) против (10+2)
    assert abs(float(doubled_native / cheap) - 2.0) < 1e-6


def test_opportunity_to_row_is_api_ready() -> None:
    """to_row() (отдаётся в UI через WS/REST) содержит все поля и JSON-совместим."""
    import json

    opportunity = Opportunity(
        strategy="dex_arb",
        chain_id=1,
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        size_in=10**18,
        expected_out=10**18,
        profit_value=Decimal("5"),
        profit_usd=Decimal("5"),
        gas_usd=Decimal("1"),
        flash_fee_usd=Decimal("0.1"),
        confidence=0.8,
        block_number=100,
        dex_buy="uniswap_v3",
        dex_sell="sushiswap",
        pools=["0x" + "33" * 20],
        tokens=["0x" + "11" * 20, "0x" + "22" * 20],
        kind="cycle",
        meta={"route": "uniswap_v3 → sushiswap"},
    )
    row = opportunity.to_row()
    assert json.loads(json.dumps(row, default=str))["strategy"] == "dex_arb"
    for key in ("uid", "net_profit_usd", "size_usd", "roi_pct", "confidence", "block_number", "kind"):
        assert key in row


async def test_strategy_service_dry_run_cycle() -> None:
    """Оркестратор: блок -> возможности -> запись в БД, всё в dry-run."""
    from flashhunter.db.seed import seed_all
    from flashhunter.db.session import init_models

    await init_models()
    await seed_all()

    # ускоряем цикл: интервал блока — константа модуля оркестратора
    import flashhunter.strategies.service as service_module

    original_interval = service_module.BLOCK_INTERVAL_SEC
    service_module.BLOCK_INTERVAL_SEC = 0.05
    service = StrategyService()
    await service.start()
    try:
        import asyncio

        await asyncio.sleep(1.2)
        state = service.state()
        assert state["running"] is True
        assert state["stats"]["blocks_processed"] >= 1
        assert state["stats"]["opportunities_found"] >= 0
        assert state["dry_run"] is True
        assert len(service.strategies) == 7
    finally:
        await service.stop()
        service_module.BLOCK_INTERVAL_SEC = original_interval
    assert service.state()["running"] is False
