"""ORM-модели FlashHunter.

Схема единая для SQLite (single-node, см. ТЗ п.7.4) и PostgreSQL (cluster):
типы подобраны так, чтобы обе СУБД принимали один и тот же DDL, а тяжёлые
timeseries-таблицы (``opportunities``/``trades``/``simulations``/``latency_metrics``)
в cluster-режиме дублируются в ClickHouse (см. migrations/002_clickhouse.sql).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


# SQLite не автоинкрементит BIGINT PRIMARY KEY, поэтому для single-node режима
# тип id понижается до INTEGER (это стандартный приём SQLAlchemy: with_variant).
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """Текущее время в UTC (все timestamps в системе — UTC, naive-хранение)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Базовый класс declarative-моделей."""

    type_annotation_map = {dict[str, Any]: JSON, list[int]: JSON, Decimal: Numeric(38, 12)}


# ---------------------------------------------------------------------------
# ENUM-ы
# ---------------------------------------------------------------------------
class OpportunityStatus(str, enum.Enum):
    """Жизненный цикл возможности."""

    detected = "detected"
    simulated = "simulated"
    rejected = "rejected"
    submitted = "submitted"
    filled = "filled"
    failed = "failed"
    expired = "expired"


class TradeStatus(str, enum.Enum):
    """Результат исполнения сделки."""

    pending = "pending"
    success = "success"
    reverted = "reverted"
    dropped = "dropped"
    cancelled = "cancelled"


class ContractStatus(str, enum.Enum):
    """Статус контракта в реестре."""

    undeployed = "undeployed"
    deployed = "deployed"
    verified = "verified"
    failed = "failed"


class StrategyKind(str, enum.Enum):
    """Типы стратегий (соответствуют python/flashhunter/strategies/*)."""

    dex_arb = "dex_arb"
    triangular = "triangular"
    stable_peg = "stable_peg"
    liquidation = "liquidation"
    jit = "jit"
    backrun = "backrun"
    intent = "intent"


class WalletKind(str, enum.Enum):
    """Кошелёк: горячий (подпись сделок) или холодный (хранение прибыли)."""

    hot = "hot"
    cold = "cold"


# ---------------------------------------------------------------------------
# Служебные таблицы (state)
# ---------------------------------------------------------------------------
class User(Base):
    """Пользователь панели управления."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="admin")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)

    sessions: Mapped[list[Session]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Session(Base):
    """Выданная JWT-сессия (хранится хэш токена для возможности отзыва)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")


class Setting(Base):
    """Key/value настройки, редактируемые из UI (в т.ч. kill-switch и лимиты)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Network(Base):
    """Поддерживаемая сеть."""

    __tablename__ = "networks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    chain_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    rpc_urls: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explorer_url: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    gas_ceiling_gwei: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("45"))
    native_symbol: Mapped[str] = mapped_column(String(16), default="ETH")


class Token(Base):
    """Токен в рамках сети."""

    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("network_id", "address", name="uq_token_network_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network_id: Mapped[int] = mapped_column(ForeignKey("networks.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    decimals: Mapped[int] = mapped_column(Integer, default=18)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Contract(Base):
    """Задеплоенный контракт комбайна (FlashLoanArbitrage / Liquidator / JITVault)."""

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network_id: Mapped[int] = mapped_column(ForeignKey("networks.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime)
    abi_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[ContractStatus] = mapped_column(
        Enum(ContractStatus, native_enum=False, length=32), default=ContractStatus.undeployed
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    tx_hash: Mapped[str | None] = mapped_column(String(80))


class Wallet(Base):
    """Кошелёк: горячий или холодный. Приватный ключ хранится только в зашифрованном виде."""

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    encrypted_key: Mapped[str | None] = mapped_column(Text)  # AES-GCM / Vault transit
    hsm_ref: Mapped[str | None] = mapped_column(String(128))
    is_hot: Mapped[bool] = mapped_column(Boolean, default=True)
    chain_scope: Mapped[list[int]] = mapped_column(JSON, default=list)
    kind: Mapped[WalletKind] = mapped_column(
        Enum(WalletKind, native_enum=False, length=16), default=WalletKind.hot
    )
    balance_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    native_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKeyEncrypted(Base):
    """API-ключи внешних провайдеров (Alchemy/QuickNode/блочные эксплореры и т.д.)."""

    __tablename__ = "api_keys_encrypted"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class StrategyConfig(Base):
    """Конфигурация стратегии: включена/выключена, параметры, сети."""

    __tablename__ = "strategies_config"
    __table_args__ = (UniqueConstraint("name", name="uq_strategy_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[StrategyKind] = mapped_column(Enum(StrategyKind, native_enum=False, length=32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    chains: Mapped[list[int]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RiskLimit(Base):
    """Лимит риск-движка: scope=global|chain|token|protocol, key — идентификатор."""

    __tablename__ = "risk_limits"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_risk_scope_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Журнал действий пользователя (критические операции: live-переключение, sweep, ключи)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------------------
# Timeseries (в cluster-режиме зеркалируется в ClickHouse)
# ---------------------------------------------------------------------------
class Opportunity(Base):
    """Найденная возможность арбитража/ликвидации/JIT/backrun."""

    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opp_ts_chain_strategy", "ts", "chain_id", "strategy"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    chain_id: Mapped[int] = mapped_column(Integer, index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    token_in: Mapped[str] = mapped_column(String(64))
    token_out: Mapped[str] = mapped_column(String(64))
    dex_buy: Mapped[str] = mapped_column(String(64), default="")
    dex_sell: Mapped[str] = mapped_column(String(64), default="")
    path: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    size_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    gross_profit_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    gas_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    flash_fee_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    net_profit_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[OpportunityStatus] = mapped_column(
        Enum(OpportunityStatus, native_enum=False, length=32), default=OpportunityStatus.detected
    )
    sim_latency_us: Mapped[int] = mapped_column(Integer, default=0)
    block_number: Mapped[int | None] = mapped_column(BigInteger)


class Trade(Base):
    """Отправленная/исполненная сделка (bundle)."""

    __tablename__ = "trades"
    __table_args__ = (Index("ix_trade_ts_chain", "ts", "chain_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chain_id: Mapped[int] = mapped_column(Integer, index=True)
    strategy: Mapped[str] = mapped_column(String(32), default="")
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    bundle_hash: Mapped[str | None] = mapped_column(String(80))
    relay: Mapped[str | None] = mapped_column(String(32))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    gas_used: Mapped[int] = mapped_column(BigInteger, default=0)
    gas_price_gwei: Mapped[float] = mapped_column(Float, default=0.0)
    priority_fee_gwei: Mapped[float] = mapped_column(Float, default=0.0)
    actual_profit_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    status: Mapped[TradeStatus] = mapped_column(
        Enum(TradeStatus, native_enum=False, length=32), default=TradeStatus.pending
    )
    revert_reason: Mapped[str | None] = mapped_column(String(256))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Детали боевого исполнения (JSON-строка): pre-flight, ответ релея, что именно
    # вернул блокчейн. Хранится текстом — одинаково работает в SQLite и Postgres.
    details: Mapped[str | None] = mapped_column(Text)


class Simulation(Base):
    """Результат симуляции кандидата (латентность и причина revert)."""

    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    latency_us: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    revert_reason: Mapped[str | None] = mapped_column(String(256))
    gas_used: Mapped[int] = mapped_column(BigInteger, default=0)


class LatencyMetric(Base):
    """Замеры латентности по сервисам (p50/p95/p99 в микросекундах)."""

    __tablename__ = "latency_metrics"
    __table_args__ = (Index("ix_latency_ts_service", "ts", "service"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(48))
    metric: Mapped[str] = mapped_column(String(48))
    p50_us: Mapped[int] = mapped_column(Integer, default=0)
    p95_us: Mapped[int] = mapped_column(Integer, default=0)
    p99_us: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=0)


class DailyStat(Base):
    """Агрегат по дням: PnL, число сделок, win/loss."""

    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("date", "chain_id", "strategy", name="uq_daily"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    chain_id: Mapped[int] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(32))
    gross_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    net_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    gas_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))


class ServiceHealth(Base):
    """Пульс микросервисов (ingestor, sim-engine, relays...) для /dashboard и /latency."""

    __tablename__ = "service_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(48), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
