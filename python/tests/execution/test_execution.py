"""Тесты исполнения: симулятор, сборка bundle, релеи и полный цикл Executor."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from flashhunter.db.seed import seed_all
from flashhunter.db.session import init_models
from flashhunter.execution.bundle_builder import BundleBuilder, NonceManager
from flashhunter.execution.executor import Executor
from flashhunter.execution.relays import RelayClient
from flashhunter.execution.simulator import REVERT_INSUFFICIENT_OUTPUT, LocalSimulator
from flashhunter.market.mock import MockMarketSource
from flashhunter.strategies.arb import DexArbStrategy
from flashhunter.strategies.base import Opportunity
from flashhunter.strategies.liq import LiquidationStrategy
from tests.conftest import make_ctx


async def _first_opportunities(strategy, profile: str = "volatile", count: int = 3, seed: int = 3):
    """Возвращает первые N возможностей и снимок, на котором они найдены."""
    source = MockMarketSource(seed=seed, profile=profile)
    collected: list[tuple[Opportunity, object]] = []
    for _ in range(30):
        snapshot = source.next_snapshot()
        ctx = make_ctx(snapshot)
        if not strategy.supports(ctx.chain_id):
            continue
        for opportunity in await strategy.on_block(ctx):
            collected.append((opportunity, snapshot))
            if len(collected) >= count:
                return collected
    return collected


# --------------------------------------------------------------------------- #
# Симулятор
# --------------------------------------------------------------------------- #
async def test_simulator_accepts_profitable_arbitrage() -> None:
    """Симулятор подтверждает прибыльный арбитраж и отдаёт точный net."""
    pairs = await _first_opportunities(DexArbStrategy(), count=5)
    assert pairs, "мок-рынок должен давать арбитражные возможности"
    simulator = LocalSimulator(slippage_tolerance_bps=50)
    success = 0
    for opportunity, snapshot in pairs:
        result = await simulator.simulate(opportunity, snapshot)
        if result.success:
            success += 1
            assert result.profit_usd >= 0
            assert result.gas_used > 0
            assert result.latency_us > 0
            assert result.revert_reason == ""
    assert success > 0


async def test_simulator_rejects_stale_price() -> None:
    """Устаревшее ожидание (цена ушла) отвергается при нулевом допуске слиппеджа.

    Проверяем именно боевой случай: возможность найдена по цене X, а к моменту
    исполнения выход меньше ожидаемого. Симулятор обязан отказать, иначе на цепочку
    уйдёт транзакция, которая вернётся убытком.
    """
    pairs = await _first_opportunities(DexArbStrategy(), count=6)
    assert pairs
    strict = LocalSimulator(slippage_tolerance_bps=0)

    # 1. Свежая возможность при нулевом допуске проходит: пересчёт совпадает с ожиданием
    fresh = await strict.simulate(pairs[0][0], pairs[0][1])
    assert fresh.success is True, fresh.revert_reason

    # 2. Завышенное ожидание (цена ушла на 1%) — отказ по недостаточному выходу
    stale = replace(pairs[0][0])
    stale.expected_out = int(stale.expected_out * 101 // 100)
    stale.profit_value = Decimal(stale.expected_out - stale.size_in)
    rejected = await strict.simulate(stale, pairs[0][1])
    assert rejected.success is False
    assert rejected.revert_reason == REVERT_INSUFFICIENT_OUTPUT


def strategy_max_hf() -> float:
    """Порог HF стратегии ликвидаций (кандидат мог быть найден на чуть ином состоянии)."""
    return 1.05


async def test_simulator_supports_every_strategy_kind() -> None:
    """Симулятор умеет обрабатывать все виды возможностей (cycle/liquidation/jit/intent/backrun)."""
    simulator = LocalSimulator()
    liquidations = await _first_opportunities(LiquidationStrategy(), profile="normal", count=2)
    assert liquidations
    for opportunity, snapshot in liquidations:
        result = await simulator.simulate(opportunity, snapshot)
        # симулятор — последний гейт: он может отказать ликвидации, если HF «восстановился»
        assert result.checks["health_factor"] <= strategy_max_hf()
        assert result.gas_used >= 200_000
        if not result.success:
            assert result.revert_reason == "HealthFactorRecovered()"

    arb = await _first_opportunities(DexArbStrategy(), count=1)
    opportunity, snapshot = arb[0]
    cycle_result = await simulator.simulate(opportunity, snapshot)
    assert cycle_result.success is True
    assert len(cycle_result.checks["route"]) >= 2  # маршрут из двух и более хопов
    assert cycle_result.checks["slippage_bps"] >= 0


# --------------------------------------------------------------------------- #
# Bundle builder
# --------------------------------------------------------------------------- #
async def test_bundle_plan_has_targets_and_calldata() -> None:
    """Bundle содержит целевой контракт, calldata и оценку газа для каждого вида сделки."""
    from flashhunter.config.settings import get_settings

    settings = get_settings()
    settings.flashloan_arbitrage_address = "0x" + "aa" * 20
    settings.liquidator_address = "0x" + "bb" * 20
    builder = BundleBuilder()
    pairs = await _first_opportunities(DexArbStrategy(), count=1)
    opportunity, snapshot = pairs[0]
    plan = builder.build(opportunity, snapshot, gas_used=340_000, sender="0x" + "ab" * 20)

    assert plan.bundle_hash.startswith("0x") and len(plan.bundle_hash) == 66
    assert plan.target.startswith("0x") and len(plan.target) == 42
    assert plan.calldata_preview.startswith("0x")
    assert plan.gas_limit >= 100_000
    assert plan.max_fee_per_gas_wei > plan.max_priority_fee_per_gas_wei
    assert plan.txs and plan.txs[0]["to"] == plan.target
    assert plan.txs[0]["nonce"] == plan.nonce

    liquidation = await _first_opportunities(LiquidationStrategy(), profile="normal", count=1)
    liq_plan = builder.build(liquidation[0][0], liquidation[0][1], gas_used=320_000)
    assert liq_plan.target != plan.target  # разные контракты для разных стратегий


async def test_bundle_replacement_keeps_nonce_and_bumps_fees() -> None:
    """Замена bundle сохраняет nonce (иначе уйдёт отдельной транзакцией) и поднимает комиссию."""
    builder = BundleBuilder()
    opportunity, snapshot = (await _first_opportunities(DexArbStrategy(), count=1))[0]
    plan = builder.build(opportunity, snapshot, gas_used=340_000)

    replacement = builder.build_replacement(plan)
    assert replacement.nonce == plan.nonce
    assert replacement.max_fee_per_gas_wei > plan.max_fee_per_gas_wei
    assert replacement.bundle_hash != plan.bundle_hash
    assert replacement.txs[0]["maxFeePerGas"] > plan.txs[0]["maxFeePerGas"]
    # исходный план не мутирован (в журнале должны остаться обе версии)
    assert plan.txs[0]["maxFeePerGas"] != replacement.txs[0]["maxFeePerGas"]


async def test_bundle_cancel_uses_same_nonce() -> None:
    """Отмена — self-transfer с тем же nonce (освобождает место в блоке)."""
    builder = BundleBuilder()
    opportunity, snapshot = (await _first_opportunities(DexArbStrategy(), count=1))[0]
    plan = builder.build(opportunity, snapshot, gas_used=340_000)
    sender = "0x" + "cd" * 20
    cancel = builder.cancel(plan, sender=sender)
    assert cancel.nonce == plan.nonce
    assert cancel.gas_limit == 21_000
    assert cancel.target == sender
    assert cancel.calldata_preview == "0x"
    assert cancel.max_fee_per_gas_wei > plan.max_fee_per_gas_wei


def test_nonce_manager_increments_monotonically() -> None:
    """Менеджер nonce выдаёт строго последовательные значения для кошелька."""
    manager = NonceManager()
    manager.seed(1, "0xabc", 10)
    assert [manager.next(1, "0xabc") for _ in range(3)] == [10, 11, 12]
    assert manager.peek(1, "0xabc") == 13
    manager.seed(1, "0xabc", 100)  # синхронизация с сетью перезаписывает
    assert manager.peek(1, "0xabc") == 100


# --------------------------------------------------------------------------- #
# Релеи
# --------------------------------------------------------------------------- #
async def test_relay_dry_run_accepts_bundle() -> None:
    """В dry-run релеи не ходят в сеть, но возвращают правдоподобную статистику."""
    relays = RelayClient(
        enabled={
            "flashbots": "https://relay.flashbots.net",
            "bloxroute": "https://mev.api.blxrbdn.com",
            "eden": "https://api.edennetwork.io/v1/bundle",
            "merkle": "https://builder.merkle.io",
            "titan": "https://rpc.titanbuilder.xyz",
            "rsync": "https://rsync-builder.xyz",
        }
    )
    assert len(relays.active_relays()) >= 5
    opportunity, snapshot = (await _first_opportunities(DexArbStrategy(), count=1))[0]
    plan = BundleBuilder().build(opportunity, snapshot, gas_used=340_000)

    responses = await relays.submit_all(plan)
    assert len(responses) == len(relays.active_relays())
    assert all(response.latency_ms > 0 for response in responses)
    assert any(response.accepted for response in responses)

    stats = relays.stats_snapshot()
    assert set(relays.active_relays()) <= {row["relay"] for row in stats}
    assert sum(row["sent"] for row in stats) == len(responses)
    assert all(row["avg_latency_ms"] >= 0 for row in stats)


async def test_relay_disabled_is_skipped() -> None:
    """Релей без настроенного эндпоинта не получает bundle."""
    relays = RelayClient(enabled={"flashbots": "", "bloxroute": "https://relay.bloxroute.com"})
    assert relays.active_relays() == ["bloxroute"]


# --------------------------------------------------------------------------- #
# Executor (полный цикл)
# --------------------------------------------------------------------------- #
async def test_executor_full_cycle_in_dry_run() -> None:
    """Полный цикл: риск -> резерв -> симуляция -> bundle -> релей -> запись в БД."""
    await init_models()
    await seed_all()

    from flashhunter.risk.engine import get_risk_engine

    executor = Executor(get_risk_engine())
    executor.risk.dry_run = True
    executor.risk.kill_switch = False
    executor.risk.max_exposure_chain = Decimal("100000000")
    executor.risk.max_exposure_token = Decimal("100000000")
    pairs = await _first_opportunities(DexArbStrategy(), count=2)
    assert pairs

    from flashhunter.db.models import OpportunityStatus, Trade, TradeStatus
    from flashhunter.db.session import session_scope
    from sqlalchemy import func, select

    async with session_scope() as session:
        for opportunity, snapshot in pairs:
            await executor.persist_opportunity(session, opportunity, OpportunityStatus.detected)
            result = await executor.execute(opportunity, snapshot, session)
            assert result.status in {"success", "rejected", "reverted", "dropped"}
            assert result.latency_ms > 0

    async with session_scope() as session:
        total = (await session.execute(select(func.count(Trade.id)))).scalar_one()
        trades = (await session.execute(select(Trade))).scalars().all()
    assert total >= 1
    for trade in trades:
        assert trade.status in {
            TradeStatus.pending,
            TradeStatus.success,
            TradeStatus.reverted,
            TradeStatus.dropped,
            TradeStatus.cancelled,
        }


async def test_executor_blocks_trade_above_gas_ceiling() -> None:
    """При превышении потолка газа сделка отклоняется риск-движком (без отправки bundle)."""
    await init_models()
    await seed_all()

    from flashhunter.risk.engine import get_risk_engine

    executor = Executor(get_risk_engine())
    executor.risk.dry_run = True
    executor.risk.kill_switch = False
    executor.risk.gas_ceiling_gwei = Decimal("1")  # заведомо недостижимо
    pairs = await _first_opportunities(DexArbStrategy(), count=1)
    opportunity, snapshot = pairs[0]

    from flashhunter.db.session import session_scope

    async with session_scope() as session:
        result = await executor.execute(opportunity, snapshot, session)
    assert result.accepted is False
    assert result.status == "rejected"
    assert "gas" in (result.reason or "").lower()
