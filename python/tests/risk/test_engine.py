"""Тесты риск-движка: гейтинг сделок, kill-switch, экспозиция, автотриггеры."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from flashhunter.risk.engine import RiskEngine, TradeOutcome, get_risk_engine


def _engine(**overrides: object) -> RiskEngine:
    """Свежий движок без dry-run маскировки (в тестах проверяем сами лимиты)."""
    engine = RiskEngine()
    engine.dry_run = False
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


def _outcome(net: str, *, status: str = "success", size: str = "0", chain_id: int = 1, token: str = "") -> TradeOutcome:
    return TradeOutcome(
        ts=time.time(),
        chain_id=chain_id,
        profit_usd=Decimal(net),
        status=status,
        latency_ms=1.0,
        size_usd=Decimal(size),
        token=token,
    )


def _check(engine: RiskEngine, **overrides: object):
    payload: dict = {
        "chain_id": 1,
        "token_in": "0xweth",
        "size_usd": Decimal("50000"),
        "net_profit_usd": Decimal("250"),
        "base_fee_gwei": 20.0,
        "slippage_bps": 15,
    }
    payload.update(overrides)
    return engine.check(**payload)


def test_check_allows_good_trade() -> None:
    """Прибыльная сделка в пределах лимитов проходит риск-гейт."""
    engine = _engine(min_profit_usd=Decimal("10"))
    decision = _check(engine)
    assert decision.allow is True
    assert decision.reason == ""
    assert decision.checks["dry_run"] is False


@pytest.mark.parametrize(
    ("overrides", "payload", "expected_reason"),
    [
        ({"min_profit_usd": Decimal("100")}, {"net_profit_usd": Decimal("5")}, "below_min_profit"),
        ({"gas_ceiling_gwei": Decimal("30")}, {"base_fee_gwei": 90.0}, "gas_above_ceiling"),
        ({"slippage_ceiling_bps": 50}, {"slippage_bps": 120}, "slippage_above_ceiling"),
        ({"max_size_usd": Decimal("1000")}, {"size_usd": Decimal("50000")}, "size_above_max"),
    ],
)
def test_check_rejects_by_reason(overrides: dict, payload: dict, expected_reason: str) -> None:
    """Каждое нарушение лимита даёт свой код причины (он попадает в журнал сделки)."""
    engine = _engine(**overrides)
    decision = _check(engine, **payload)
    assert decision.allow is False
    assert decision.reason == expected_reason


def test_manual_kill_switch_blocks_everything() -> None:
    """Ручной kill-switch блокирует любые сделки до ручного снятия."""
    engine = _engine()
    engine.engage_kill_switch("unit test")
    decision = _check(engine, size_usd=Decimal("10"), net_profit_usd=Decimal("10000"))
    assert decision.allow is False
    assert decision.reason == "kill_switch_active"

    engine.release_kill_switch("unit test release")
    assert engine.kill_switch is False
    assert _check(engine).allow is True


@pytest.mark.parametrize(
    ("limit_attr", "reason_fragment"),
    [("daily_loss_limit", "daily_loss_limit"), ("hourly_loss_limit", "hourly_loss_limit")],
)
def test_auto_kill_on_loss_limits(limit_attr: str, reason_fragment: str) -> None:
    """Превышение дневного/часового лимита убытка взводит kill-switch автоматически."""
    engine = _engine()
    setattr(engine, "daily_loss_limit", Decimal("100000"))
    setattr(engine, "hourly_loss_limit", Decimal("100000"))
    setattr(engine, limit_attr, Decimal("100"))
    engine.record_outcome(_outcome("-500"))
    assert engine.kill_switch is True
    assert reason_fragment in engine.kill_reason


def test_auto_kill_on_revert_rate() -> None:
    """Серия ревертов выше лимита останавливает торговлю."""
    engine = _engine(revert_rate_limit=30.0)
    for _ in range(12):
        engine.record_outcome(_outcome("-1", status="reverted"))
    assert engine.kill_switch is True
    assert "revert_rate" in engine.kill_reason


def test_auto_kill_on_latency() -> None:
    """Превышение p99 латентности также останавливает торговлю."""
    engine = _engine(latency_p99_limit_ms=50.0)
    for _ in range(40):
        engine.record_latency(400.0)
    assert engine.kill_switch is True
    assert "latency" in engine.kill_reason


def test_exposure_limits_and_release() -> None:
    """Экспозиция учитывается в check(), а успешная сделка освобождает резерв."""
    engine = _engine(max_exposure_chain=Decimal("100000"), max_exposure_token=Decimal("100000"))
    engine.reserve_exposure(chain_id=1, token="0xweth", amount_usd=Decimal("60000"))

    decision = _check(engine, size_usd=Decimal("60000"))
    assert decision.allow is False
    assert decision.reason == "chain_exposure_limit"

    # успешная сделка освобождает зарезервированный объём (chain + token)
    engine.record_outcome(_outcome("10", size="60000", token="0xweth"))
    assert engine.snapshot_state()["exposure"]["chains"].get("1", 0.0) == 0.0
    assert _check(engine, size_usd=Decimal("60000")).allow is True


def test_losing_trade_keeps_exposure_reserved() -> None:
    """Реверт не освобождает экспозицию автоматически — её снимает исполнение (Executor)."""
    engine = _engine()
    engine.reserve_exposure(chain_id=1, token="0xweth", amount_usd=Decimal("1000"))
    engine.record_outcome(_outcome("-3", status="reverted", size="1000", token="0xweth"))
    assert engine.snapshot_state()["exposure"]["chains"]["1"] == 1000.0


def test_connection_watchdog() -> None:
    """Отсутствие пульса сети дольше лимита помечается как stale_chain_feed."""
    engine = _engine(connection_loss_sec=1.0)
    assert engine.check_connection().allow is True  # нет данных — нет и отказов
    engine.mark_chain_alive(1)
    assert engine.check_connection().allow is True
    engine._last_chain_seen[1] = time.time() - 5  # имитируем обрыв связи
    decision = engine.check_connection()
    assert decision.allow is False
    assert decision.reason == "stale_chain_feed"


def test_snapshot_state_is_json_serializable() -> None:
    """Снимок риск-состояния уходит в API (должен быть JSON-совместимым)."""
    import json

    engine = _engine()
    engine.record_outcome(_outcome("42.5"))
    state = engine.snapshot_state()
    assert json.loads(json.dumps(state))["daily_pnl_usd"] == 42.5
    for key in ("kill_switch", "dry_run", "daily_limit_usd", "hourly_limit_usd", "exposure"):
        assert key in state


def test_singleton_accessor_returns_engine() -> None:
    """get_risk_engine() отдаёт глобальный движок, который использует Executor."""
    engine = get_risk_engine()
    assert isinstance(engine, RiskEngine)
    assert get_risk_engine() is engine
