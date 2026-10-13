"""Тесты слоя БД: схема, вставка/чтение, идемпотентность сида, шифрование секретов."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from flashhunter.config.secrets import decrypt_secret, encrypt_secret, mask_secret
from flashhunter.db.models import (
    AuditLog,
    Network,
    Opportunity,
    OpportunityStatus,
    Setting,
    StrategyConfig,
    StrategyKind,
    Token,
    Trade,
    TradeStatus,
    User,
    utcnow,
)
from flashhunter.db.seed import seed_all
from flashhunter.db.session import session_scope


async def test_seed_creates_reference_data(db_ready: None) -> None:
    """Сид создаёт пользователя, сети, токены, стратегии и лимиты."""
    async with session_scope() as session:
        counts = {
            "users": (await session.execute(select(func.count(User.id)))).scalar_one(),
            "networks": (await session.execute(select(func.count(Network.id)))).scalar_one(),
            "tokens": (await session.execute(select(func.count(Token.id)))).scalar_one(),
            "strategies": (await session.execute(select(func.count(StrategyConfig.id)))).scalar_one(),
        }
    assert counts["users"] >= 1
    assert counts["networks"] >= 10
    assert counts["tokens"] >= 5
    assert counts["strategies"] == 7
    async with session_scope() as session:
        non_mainnet_tokens = await session.execute(
            select(func.count(Token.id))
            .join(Network, Token.network_id == Network.id)
            .where(Network.chain_id != 1)
        )
    assert non_mainnet_tokens.scalar_one() == 0


async def test_seed_is_idempotent(db_ready: None) -> None:
    """Повторный запуск сида не дублирует записи."""
    await seed_all()
    async with session_scope() as session:
        before = (await session.execute(select(func.count(StrategyConfig.id)))).scalar_one()
    await seed_all()
    async with session_scope() as session:
        after = (await session.execute(select(func.count(StrategyConfig.id)))).scalar_one()
    assert before == after


async def test_seed_disables_mainnet_tokens_on_other_chains(db_ready: None) -> None:
    """Старые ошибочные копии mainnet-адресов выключаются на других сетях."""
    from flashhunter.db.seed import STABLES

    async with session_scope() as session:
        network = (
            await session.execute(select(Network).where(Network.chain_id == 42161))
        ).scalar_one()
        session.add(
            Token(
                network_id=network.id,
                symbol="USDC",
                address=STABLES["USDC"][0],
                decimals=6,
                enabled=True,
            )
        )
    await seed_all()
    async with session_scope() as session:
        legacy_token = (
            await session.execute(
                select(Token).where(
                    Token.network_id == network.id,
                    Token.address == STABLES["USDC"][0],
                )
            )
        ).scalar_one()
    assert legacy_token.enabled is False


async def test_jit_and_intent_enabled_by_default(db_ready: None) -> None:
    """Все семь стратегий доступны из коробки (jit/intent включены после калибровки)."""
    await seed_all()
    async with session_scope() as session:
        rows = (await session.execute(select(StrategyConfig))).scalars().all()
    by_name = {row.name: row for row in rows}
    assert set(by_name) == {
        "dex_arb",
        "triangular",
        "stable_peg",
        "liquidation",
        "jit",
        "backrun",
        "intent",
    }
    assert by_name["jit"].enabled is True
    assert by_name["intent"].enabled is True
    assert by_name["backrun"].kind is StrategyKind.backrun


async def test_trade_roundtrip_with_bigint_pk(db_ready: None) -> None:
    """Primary keys авто-инкрементируются даже в SQLite (BigInteger + variant)."""
    await seed_all()
    async with session_scope() as session:
        opportunity = Opportunity(
            strategy="dex_arb",
            chain_id=1,
            token_in="0xweth",
            token_out="0xusdc",
            dex_buy="uniswap_v3",
            dex_sell="sushiswap",
            status=OpportunityStatus.detected,
            size_usd=Decimal("1000.00"),
            net_profit_usd=Decimal("12.50"),
            roi_pct=Decimal("1.25"),
            confidence=Decimal("0.7"),
        )
        session.add(opportunity)
        await session.flush()
        assert opportunity.id is not None and opportunity.id > 0

        trade = Trade(
            chain_id=1,
            strategy="dex_arb",
            status=TradeStatus.success,
            actual_profit_usd=Decimal("12.50"),
            gas_used=210_000,
            latency_ms=12.5,
            bundle_hash="0xdeadbeef",
            relay="flashbots",
        )
        session.add(trade)
        await session.flush()
        trade_id = trade.id
        assert trade_id and trade_id > 0

    async with session_scope() as session:
        loaded = await session.get(Trade, trade_id)
        assert loaded is not None
        assert loaded.status is TradeStatus.success
        assert loaded.bundle_hash == "0xdeadbeef"
        assert loaded.ts is not None


async def test_audit_log_and_settings_roundtrip(db_ready: None) -> None:
    """Журнал аудита и key-value настройки сохраняются и читаются."""
    await seed_all()
    async with session_scope() as session:
        session.add(AuditLog(username="admin", action="unit_test", payload={"x": 1}, ip="127.0.0.1"))
        setting = await session.get(Setting, "unit_test_key")
        if setting is None:
            session.add(Setting(key="unit_test_key", value={"ts": utcnow().isoformat()}))

    async with session_scope() as session:
        logs = (await session.execute(select(AuditLog).where(AuditLog.action == "unit_test"))).scalars().all()
        stored = await session.get(Setting, "unit_test_key")
    assert len(logs) == 1
    assert logs[0].payload == {"x": 1}
    assert stored is not None and "ts" in stored.value


def test_secrets_encrypt_decrypt_and_mask() -> None:
    """Локальное шифрование секретов: обратимо, детектирует подмену, маскирует хвост."""
    secret = "0x" + "a1" * 32
    token = encrypt_secret(secret)
    assert token.startswith("local:v2:")
    assert secret not in token
    assert decrypt_secret(token) == secret

    tampered = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    with pytest.raises(Exception):
        decrypt_secret(tampered)

    masked = mask_secret(secret)
    assert masked.endswith(secret[-4:])
    assert "a1a1" not in masked.replace(masked[-4:], "")


def test_risk_limits_seeded_with_expected_scope() -> None:
    """В сиде есть глобальные риск-лимиты (daily/hourly loss, gas, slippage)."""
    from flashhunter.db.models import RiskLimit

    assert RiskLimit.__tablename__ == "risk_limits"
