"""Конфигурация FlashHunter.

Все параметры читаются из переменных окружения / файла ``.env`` через
``pydantic-settings``. Секреты в production подгружаются из Vault (см. ``load_secrets``),
но структура настроек остаётся той же.
"""

from __future__ import annotations

import ipaddress
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChainRpc(BaseSettings):
    """Параметры доступа к сети: свои ноды + fallback-провайдеры."""

    chain_id: int
    name: str
    http: str = ""
    ws: str = ""
    explorer: str = ""
    native_symbol: str = "ETH"
    enabled: bool = True
    gas_ceiling_gwei: Decimal = Decimal("45")


class Settings(BaseSettings):
    """Единая точка конфигурации. Поле -> переменная окружения (префикс FLASHHUNTER_ опционален)."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 1. Общее ----------------------------------------------------------
    env: Literal["dev", "staging", "prod"] = Field(default="dev", alias="FLASHHUNTER_ENV")
    log_level: str = Field(default="INFO", alias="FLASHHUNTER_LOG_LEVEL")
    log_json: bool = Field(default=False, alias="FLASHHUNTER_LOG_JSON")
    instance_id: str = Field(default="fh-single-01", alias="FLASHHUNTER_INSTANCE_ID")
    dry_run: bool = Field(default=True, alias="FLASHHUNTER_DRY_RUN")
    auto_open_browser: bool = Field(default=True, alias="FLASHHUNTER_AUTO_OPEN_BROWSER")
    force_password_change: bool = Field(default=True, alias="FLASHHUNTER_FORCE_PASSWORD_CHANGE")

    # --- 2. Хранилища ------------------------------------------------------
    db_mode: Literal["sqlite", "postgres"] = Field(default="sqlite", alias="FLASHHUNTER_DB_MODE")
    sqlite_path: str = Field(default="/data/flashhunter.db", alias="SQLITE_PATH")
    postgres_dsn: str = Field(
        default="postgresql+asyncpg://flashhunter:flashhunter@postgres:5432/flashhunter",
        alias="POSTGRES_DSN",
    )
    clickhouse_dsn: str = Field(default="", alias="CLICKHOUSE_DSN")
    redis_url: str = Field(default="", alias="REDIS_URL")
    nats_url: str = Field(default="", alias="NATS_URL")

    # --- 3. API / UI -------------------------------------------------------
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    jwt_secret: SecretStr = Field(
        default=SecretStr("dev-insecure-secret-please-rotate-0000000000000000000000000000"),
        alias="JWT_SECRET",
    )
    jwt_alg: str = Field(default="HS256", alias="JWT_ALG")
    jwt_ttl_minutes: int = Field(default=720, alias="JWT_TTL_MINUTES")
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    admin_default_user: str = Field(default="admin", alias="ADMIN_DEFAULT_USER")
    admin_default_password: SecretStr = Field(default=SecretStr("admin"), alias="ADMIN_DEFAULT_PASSWORD")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"], alias="CORS_ORIGINS")

    # --- 4. RPC ------------------------------------------------------------
    eth_rpc_http: str = Field(default="", alias="ETH_RPC_HTTP")
    # Запасные публичные узлы (без ключей): нужны, когда приватный RPC не настроен.
    # Проверены на реальных вызовах; приватный узел из ETH_RPC_HTTP всегда впереди.
    eth_rpc_fallbacks: str = Field(
        default="https://eth.drpc.org,https://eth-mainnet.public.blastapi.io,"
        "https://ethereum-rpc.publicnode.com,https://rpc.flashbots.net",
        alias="ETH_RPC_FALLBACKS",
    )
    # Живой движок: какая сеть наблюдается, как глубоко сканировать позиции Aave
    # и как часто обновлять список заёмщиков (каждый блок — дорого по RPC).
    live_chain_id: int = Field(default=1, alias="LIVE_CHAIN_ID")
    live_aave_lookback_blocks: int = Field(default=900, ge=0, le=20000, alias="LIVE_AAVE_LOOKBACK_BLOCKS")
    live_aave_max_users: int = Field(default=150, ge=1, le=2000, alias="LIVE_AAVE_MAX_USERS")
    live_borrower_refresh_sec: float = Field(default=60.0, ge=5.0, le=3600.0, alias="LIVE_BORROWER_REFRESH_SEC")
    # Health factor, ниже которого позиция Aave держится в постоянном наблюдении
    live_watch_hf_threshold: float = Field(default=1.25, ge=1.0, le=5.0, alias="LIVE_WATCH_HF_THRESHOLD")
    # Поток ожидающих транзакций (backrun/JIT) включается только на узле, который его
    # выдержит: публичные WS не тянут такую подписку и начинают резать остальные запросы.
    live_ws_pending_enabled: bool = Field(default=False, alias="LIVE_WS_PENDING_TXS")
    live_price_ttl_sec: float = Field(default=30.0, ge=5.0, le=600.0, alias="LIVE_PRICE_TTL_SEC")
    # Ожидание между обработками блоков в живой сети, с. У mainnet блок ~12 с;
    # 1–2 секунды означает «идти в ногу с сетью», не пропуская и не дублируя блоки.
    block_interval_sec: float = Field(default=2.0, ge=0.5, le=60.0, alias="LIVE_BLOCK_INTERVAL_SEC")
    eth_rpc_ws: str = Field(default="", alias="ETH_RPC_WS")
    eth_flashbots_relay: str = Field(
        default="https://relay.flashbots.net", alias="ETH_FLASHBOTS_RELAY"
    )
    bsc_rpc_http: str = Field(default="", alias="BSC_RPC_HTTP")
    arb_rpc_http: str = Field(default="", alias="ARB_RPC_HTTP")
    base_rpc_http: str = Field(default="", alias="BASE_RPC_HTTP")
    op_rpc_http: str = Field(default="", alias="OP_RPC_HTTP")
    polygon_rpc_http: str = Field(default="", alias="POLYGON_RPC_HTTP")
    avalanche_rpc_http: str = Field(default="", alias="AVALANCHE_RPC_HTTP")

    # --- 5. Релеи ----------------------------------------------------------
    flashbots_auth_key: SecretStr = Field(default=SecretStr(""), alias="FLASHBOTS_AUTH_KEY")
    bloxroute_relay_url: str = Field(default="", alias="BLOXROUTE_RELAY_URL")
    eden_relay_url: str = Field(default="", alias="EDEN_RELAY_URL")
    merkle_relay_url: str = Field(default="", alias="MERKLE_RELAY_URL")
    titan_relay_url: str = Field(default="", alias="TITAN_RELAY_URL")
    rsync_relay_url: str = Field(default="", alias="RSYNC_RELAY_URL")
    beaver_relay_url: str = Field(default="", alias="BEAVER_RELAY_URL")
    buildernet_relay_url: str = Field(default="", alias="BUILDERNET_RELAY_URL")
    protect_rpc_url: str = Field(default="", alias="PROTECT_RPC_URL")
    relay_timeout_ms: int = Field(default=400, alias="RELAY_TIMEOUT_MS")
    relay_max_inflight: int = Field(default=64, alias="RELAY_MAX_INFLIGHT")

    # --- 6. Контракты ------------------------------------------------------
    flashloan_arbitrage_address: str = Field(default="", alias="FLASHLOAN_ARBITRAGE_ADDRESS")
    liquidator_address: str = Field(default="", alias="LIQUIDATOR_ADDRESS")
    jit_vault_address: str = Field(default="", alias="JIT_VAULT_ADDRESS")

    # --- 7. Исполнение -----------------------------------------------------
    exec_max_gas_gwei: Decimal = Field(default=Decimal("45"), alias="EXEC_MAX_GAS_GWEI")
    exec_priority_fee_gwei: Decimal = Field(default=Decimal("2"), alias="EXEC_PRIORITY_FEE_GWEI")
    exec_replacement_bump_pct: int = Field(default=15, alias="EXEC_REPLACEMENT_BUMP_PCT")
    exec_max_slippage_bps: int = Field(default=30, alias="EXEC_MAX_SLIPPAGE_BPS")
    exec_min_profit_usd: Decimal = Field(default=Decimal("12"), alias="EXEC_MIN_PROFIT_USD")
    exec_max_size_usd: Decimal = Field(default=Decimal("250000"), alias="EXEC_MAX_SIZE_USD")
    exec_sim_timeout_ms: int = Field(default=40, alias="EXEC_SIM_TIMEOUT_MS")
    exec_receipt_timeout_sec: float = Field(default=30.0, alias="EXEC_RECEIPT_TIMEOUT_SEC")
    exec_confirmations: int = Field(default=1, ge=1, le=12, alias="EXEC_CONFIRMATIONS")

    # --- 7b. Ключ кипера: только для боевой отправки ------------------------
    # Ровно одна из двух переменных должна быть задана: либо сам ключ, либо файл
    # с ключом/keystore (права 0400). Пусто = режим readonly: сделки не отправляются.
    keeper_private_key: SecretStr = Field(default=SecretStr(""), alias="KEEPER_PRIVATE_KEY")
    keeper_key_file: str = Field(default="", alias="KEEPER_KEY_FILE")

    # --- 8. Риск -----------------------------------------------------------
    risk_daily_loss_limit_usd: Decimal = Field(default=Decimal("500"), alias="RISK_DAILY_LOSS_LIMIT_USD")
    risk_hourly_loss_limit_usd: Decimal = Field(default=Decimal("150"), alias="RISK_HOURLY_LOSS_LIMIT_USD")
    risk_revert_rate_limit_pct: Decimal = Field(default=Decimal("20"), alias="RISK_REVERT_RATE_LIMIT_PCT")
    risk_latency_p99_limit_ms: int = Field(default=500, alias="RISK_LATENCY_P99_LIMIT_MS")
    risk_connection_loss_sec: int = Field(default=10, alias="RISK_CONNECTION_LOSS_SEC")
    risk_max_exposure_per_chain_usd: Decimal = Field(
        default=Decimal("150000"), alias="RISK_MAX_EXPOSURE_PER_CHAIN_USD"
    )
    risk_max_exposure_per_token_usd: Decimal = Field(
        default=Decimal("75000"), alias="RISK_MAX_EXPOSURE_PER_TOKEN_USD"
    )
    risk_auto_sweep_interval_sec: int = Field(default=3600, alias="RISK_AUTO_SWEEP_INTERVAL_SEC")
    cold_wallet_address: str = Field(default="", alias="COLD_WALLET_ADDRESS")
    sweep_min_amount_usd: Decimal = Field(default=Decimal("200"), alias="SWEEP_MIN_AMOUNT_USD")

    # --- 9. Стратегии ------------------------------------------------------
    strat_arb_enabled: bool = Field(default=True, alias="STRAT_ARB_ENABLED")
    strat_arb_chains: list[int] = Field(default_factory=lambda: [1, 8453, 42161], alias="STRAT_ARB_CHAINS")
    strat_triangular_enabled: bool = Field(default=True, alias="STRAT_TRIANGULAR_ENABLED")
    strat_stable_peg_enabled: bool = Field(default=True, alias="STRAT_STABLE_PEG_ENABLED")
    strat_liq_enabled: bool = Field(default=True, alias="STRAT_LIQ_ENABLED")
    strat_jit_enabled: bool = Field(default=False, alias="STRAT_JIT_ENABLED")
    strat_backrun_enabled: bool = Field(default=True, alias="STRAT_BACKRUN_ENABLED")
    strat_intent_enabled: bool = Field(default=False, alias="STRAT_INTENT_ENABLED")
    # false (по умолчанию) — данные читаются из реальной сети; true — локальная имитация
    # для офлайн-разработки и тестов (см. flashhunter.market.mock)
    mock_market_data: bool = Field(default=False, alias="MOCK_MARKET_DATA")

    # --- 10. Наблюдаемость --------------------------------------------------
    prometheus_port: int = Field(default=9100, alias="PROMETHEUS_PORT")
    alerting_enabled: bool = Field(default=True, alias="ALERTING_ENABLED")
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")

    # --- 10.1 Алертинг: каналы уведомлений ---------------------------------
    alert_telegram_token: SecretStr = Field(default=SecretStr(""), alias="ALERT_TELEGRAM_TOKEN")
    alert_telegram_chat_id: str = Field(default="", alias="ALERT_TELEGRAM_CHAT_ID")
    alert_discord_webhook: str = Field(default="", alias="ALERT_DISCORD_WEBHOOK")
    alert_webhook_url: str = Field(default="", alias="ALERT_WEBHOOK_URL")
    alert_timeout_ms: int = Field(default=5000, ge=500, le=60_000, alias="ALERT_TIMEOUT_MS")

    # ------------------------------------------------------------------ #
    # Валидаторы
    # ------------------------------------------------------------------ #
    @field_validator("cors_origins", "strat_arb_chains", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Разрешает список как из CSV-строки (для .env), так и из JSON-массива."""
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return value  # pydantic распарсит JSON
            return [part.strip() for part in raw.split(",") if part.strip()]
        return value

    # ------------------------------------------------------------------ #
    # Производные настройки живого режима
    # ------------------------------------------------------------------ #
    def rpc_endpoints(self, chain_id: int = 1) -> list[str]:
        """Список RPC-эндпоинтов для сети: приватный (если задан) + публичные запасные.

        Приватный узел из .env всегда первый: он быстрее и с лимитами выше.
        Публичные нужны, чтобы система работала «из коробки» без ключей.
        """
        private_map = {
            1: self.eth_rpc_http,
            42161: self.arb_rpc_http,
            8453: self.base_rpc_http,
            10: self.op_rpc_http,
            137: self.polygon_rpc_http,
            56: self.bsc_rpc_http,
            43114: self.avalanche_rpc_http,
        }
        urls: list[str] = []
        private = private_map.get(chain_id, "")
        if private:
            urls.append(private)
        if chain_id == 1:  # публичные запасные проверены на mainnet
            urls.extend(part.strip() for part in self.eth_rpc_fallbacks.split(",") if part.strip())
        return urls

    def ws_endpoint(self) -> str:
        """WebSocket-эндпоинт мемпула: только явно заданный в конфигурации.

        Публичный WS-хост не используется намеренно. Поток ожидающих транзакций —
        это сотни сообщений в секунду; публичные узлы шейпят такой поток, и провайдер
        начинает резать уже обычные HTTP-запросы того же хоста. В результате снимок
        рынка теряет пулы, а мемпула всё равно нет. Без приватного WS источником
        свопов служит разбор свежих блоков (``_swaps_loop``) — он не требует подписки.
        """
        return self.eth_rpc_ws

    @property
    def live_ws_pending_txs(self) -> bool:
        """Слушать ли поток ожидающих транзакций (нужен узел, который его выдержит)."""
        return bool(self.live_ws_pending_enabled)

    @model_validator(mode="after")
    def _sanity(self) -> Settings:
        """Проверки инвариантов: prod-режим не допускает небезопасных значений по умолчанию."""
        if self.env == "prod":
            if self.jwt_secret.get_secret_value().startswith("dev-insecure"):
                raise ValueError("JWT_SECRET must be rotated before running in prod")
            if self.admin_default_password.get_secret_value() == "admin":
                raise ValueError("ADMIN_DEFAULT_PASSWORD must be changed in prod")
            if self.mock_market_data:
                raise ValueError("MOCK_MARKET_DATA must be false in prod")
            if not self.rpc_endpoints(self.live_chain_id):
                raise ValueError("A live RPC endpoint is required for the configured chain in prod")
        if self.exec_min_profit_usd < 0:
            raise ValueError("EXEC_MIN_PROFIT_USD must be >= 0")
        return self

    # ------------------------------------------------------------------ #
    # Производные свойства
    # ------------------------------------------------------------------ #
    @property
    def alert_telegram_token_str(self) -> str:
        """Токен Telegram-бота в виде строки (SecretStr прячет значение в логах)."""
        return self.alert_telegram_token.get_secret_value()

    @property
    def is_single_node(self) -> bool:
        """True — режим одного узла (SQLite, без кластера Postgres/CH/Redis/NATS)."""
        return self.db_mode == "sqlite"

    @property
    def database_url(self) -> str:
        """DSN для SQLAlchemy async engine."""
        if self.db_mode == "postgres":
            return self.postgres_dsn
        path = Path(self.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}"

    def chains(self) -> list[ChainRpc]:
        """Активные сети с настроенным RPC (используются API и orchestration-слоем)."""
        catalog = [
            ChainRpc(chain_id=1, name="ethereum", http=self.eth_rpc_http, ws=self.eth_rpc_ws,
                     explorer="https://etherscan.io", native_symbol="ETH",
                     gas_ceiling_gwei=self.exec_max_gas_gwei),
            ChainRpc(chain_id=56, name="bsc", http=self.bsc_rpc_http,
                     explorer="https://bscscan.com", native_symbol="BNB"),
            ChainRpc(chain_id=42161, name="arbitrum", http=self.arb_rpc_http,
                     explorer="https://arbiscan.io", native_symbol="ETH"),
            ChainRpc(chain_id=8453, name="base", http=self.base_rpc_http,
                     explorer="https://basescan.org", native_symbol="ETH"),
            ChainRpc(chain_id=10, name="optimism", http=self.op_rpc_http,
                     explorer="https://optimistic.etherscan.io", native_symbol="ETH"),
            ChainRpc(chain_id=137, name="polygon", http=self.polygon_rpc_http,
                     explorer="https://polygonscan.com", native_symbol="POL"),
            ChainRpc(chain_id=43114, name="avalanche", http=self.avalanche_rpc_http,
                     explorer="https://snowtrace.io", native_symbol="AVAX"),
        ]
        return [c for c in catalog if c.enabled and c.http]

    def keeper_key(self) -> str:
        """Приватный ключ кипера: переменная окружения либо файл (см. chain.signer).

        Возвращаемое значение никогда не логируется и не сохраняется: вызывающая
        сторона обязана передать его сразу в KeeperWallet.
        """
        from flashhunter.chain.signer import load_keeper_key

        return load_keeper_key(self.keeper_private_key.get_secret_value(), self.keeper_key_file)

    def live_relays(self) -> dict[str, str]:
        """Словарь активных релеев: имя -> URL (пустые значения отбрасываются)."""
        candidates = {
            "flashbots": self.eth_flashbots_relay,
            "bloxroute": self.bloxroute_relay_url,
            "eden": self.eden_relay_url,
            "merkle": self.merkle_relay_url,
            "titan": self.titan_relay_url,
            "rsync": self.rsync_relay_url,
            "beaver": self.beaver_relay_url,
            "buildernet": self.buildernet_relay_url,
        }
        return {name: url for name, url in candidates.items() if url}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек (переиспользуется API, оркестратором и стратегиями)."""
    return Settings()


def is_private_host(host: str) -> bool:
    """True, если хост — приватный/локальный адрес (используется в проверках безопасности RPC)."""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host in {"localhost", "reth", "geth", "erigon", "postgres", "redis", "nats"}
