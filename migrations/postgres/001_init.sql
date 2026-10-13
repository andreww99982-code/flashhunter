-- FlashHunter: прод-схема PostgreSQL. Отличия от SQLite-версии:
--   * TIMESTAMPTZ вместо TEXT для времени (корректные интервалы и таймзоны);
--   * NUMERIC/BIGINT для денег и газа;
--   * партиционирование высокочастотных таблиц по времени (BRIN-индексы);
--   * JSONB вместо TEXT для полезных нагрузок.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id                   BIGSERIAL PRIMARY KEY,
    username             TEXT        NOT NULL UNIQUE,
    password_hash        TEXT        NOT NULL,
    role                 TEXT        NOT NULL DEFAULT 'admin',
    totp_secret          TEXT,
    totp_enabled         BOOLEAN     NOT NULL DEFAULT FALSE,
    must_change_password BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS networks (
    id              BIGSERIAL PRIMARY KEY,
    chain_id        BIGINT      NOT NULL UNIQUE,
    name            TEXT        NOT NULL,
    native_symbol   TEXT        NOT NULL,
    rpc_http        TEXT,
    rpc_ws          TEXT,
    explorer_url    TEXT,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    median_block_ms INTEGER     NOT NULL DEFAULT 12000,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tokens (
    id          BIGSERIAL PRIMARY KEY,
    chain_id    BIGINT      NOT NULL,
    address     TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    decimals    INTEGER     NOT NULL DEFAULT 18,
    is_stable   BOOLEAN     NOT NULL DEFAULT FALSE,
    price_usd   NUMERIC(24, 8),
    UNIQUE (chain_id, address)
);
CREATE INDEX IF NOT EXISTS idx_tokens_symbol ON tokens(symbol);

CREATE TABLE IF NOT EXISTS pools (
    id            BIGSERIAL PRIMARY KEY,
    chain_id      BIGINT      NOT NULL,
    address       TEXT        NOT NULL,
    dex           TEXT        NOT NULL,
    kind          TEXT        NOT NULL,
    token0        TEXT        NOT NULL,
    token1        TEXT        NOT NULL,
    fee_bps       INTEGER     NOT NULL DEFAULT 30,
    reserve0      NUMERIC(78, 0),
    reserve1      NUMERIC(78, 0),
    liquidity_usd NUMERIC(24, 8) NOT NULL DEFAULT 0,
    updated_block BIGINT,
    updated_at    TIMESTAMPTZ,
    UNIQUE (chain_id, address)
);
CREATE INDEX IF NOT EXISTS idx_pools_pair ON pools(chain_id, token0, token1);
CREATE INDEX IF NOT EXISTS idx_pools_liquidity ON pools(liquidity_usd DESC);

CREATE TABLE IF NOT EXISTS strategy_configs (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    chains      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    params_json JSONB       NOT NULL DEFAULT '{}'::jsonb,
    priority    INTEGER     NOT NULL DEFAULT 100,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS opportunities (
    id             BIGSERIAL PRIMARY KEY,
    uid            TEXT        NOT NULL UNIQUE,
    strategy       TEXT        NOT NULL,
    chain_id       BIGINT      NOT NULL,
    block_number   BIGINT,
    token_in       TEXT        NOT NULL,
    token_out      TEXT        NOT NULL,
    dex_buy        TEXT        NOT NULL,
    dex_sell       TEXT        NOT NULL,
    size_usd       NUMERIC(24, 8) NOT NULL DEFAULT 0,
    profit_usd     NUMERIC(24, 8) NOT NULL DEFAULT 0,
    net_profit_usd NUMERIC(24, 8) NOT NULL DEFAULT 0,
    gas_usd        NUMERIC(24, 8) NOT NULL DEFAULT 0,
    flash_fee_usd  NUMERIC(24, 8) NOT NULL DEFAULT 0,
    roi_pct        NUMERIC(18, 8) NOT NULL DEFAULT 0,
    confidence     NUMERIC(8, 4)  NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'found',
    reason         TEXT,
    latency_us     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);
CREATE INDEX IF NOT EXISTS idx_opps_strategy ON opportunities(strategy, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opps_status ON opportunities(status);

-- Партиции: текущий месяц + два следующих создаёт cron-задача (см. 002-миграцию).
CREATE TABLE IF NOT EXISTS opportunities_default PARTITION OF opportunities DEFAULT;

CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_uid TEXT,
    strategy        TEXT        NOT NULL,
    chain_id        BIGINT      NOT NULL,
    status          TEXT        NOT NULL,
    tx_hash         TEXT,
    bundle_hash     TEXT,
    relay           TEXT,
    nonce           BIGINT,
    gas_used        BIGINT,
    gas_price_gwei  NUMERIC(18, 4),
    profit_usd      NUMERIC(24, 8) NOT NULL DEFAULT 0,
    size_usd        NUMERIC(24, 8) NOT NULL DEFAULT 0,
    dry_run         BOOLEAN     NOT NULL DEFAULT TRUE,
    block_number    BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    included_at     TIMESTAMPTZ
) PARTITION BY RANGE (created_at);
CREATE TABLE IF NOT EXISTS trades_default PARTITION OF trades DEFAULT;

CREATE TABLE IF NOT EXISTS wallets (
    id               BIGSERIAL PRIMARY KEY,
    label            TEXT        NOT NULL,
    address          TEXT        NOT NULL UNIQUE,
    kind             TEXT        NOT NULL DEFAULT 'hot',
    chain_id         BIGINT      NOT NULL DEFAULT 1,
    balance_usd      NUMERIC(24, 8) NOT NULL DEFAULT 0,
    max_exposure_usd NUMERIC(24, 8) NOT NULL DEFAULT 10000,
    enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_limits (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT        NOT NULL UNIQUE,
    value_num   NUMERIC(24, 8) NOT NULL,
    unit        TEXT        NOT NULL DEFAULT 'usd',
    description TEXT,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT        NOT NULL UNIQUE,
    value      TEXT        NOT NULL,
    is_secret  BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    label        TEXT        NOT NULL,
    key_preview  TEXT        NOT NULL,
    key_hash     TEXT        NOT NULL UNIQUE,
    scopes       JSONB       NOT NULL DEFAULT '["read"]'::jsonb,
    enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id   TEXT        NOT NULL UNIQUE,
    user_agent TEXT,
    ip         TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS contracts (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT        NOT NULL,
    chain_id   BIGINT      NOT NULL,
    address    TEXT,
    deploy_tx  TEXT,
    verified   BOOLEAN     NOT NULL DEFAULT FALSE,
    abi_json   JSONB,
    version    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, chain_id)
);

-- Высокочастотные таблицы: без партиционирования, но с BRIN — они пишутся постоянно
CREATE TABLE IF NOT EXISTS latency_metrics (
    id            BIGSERIAL PRIMARY KEY,
    service       TEXT        NOT NULL,
    chain_id      BIGINT,
    stage         TEXT        NOT NULL,
    latency_us    INTEGER     NOT NULL,
    budget_us     INTEGER     NOT NULL,
    within_budget BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_latency_brin ON latency_metrics USING brin (created_at);
CREATE INDEX IF NOT EXISTS idx_latency_service ON latency_metrics(service, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    actor        TEXT        NOT NULL,
    action       TEXT        NOT NULL,
    target       TEXT,
    payload_json JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ip           TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_brin ON audit_log USING brin (created_at);

CREATE TABLE IF NOT EXISTS event_log (
    id           BIGSERIAL PRIMARY KEY,
    level        TEXT        NOT NULL DEFAULT 'info',
    source       TEXT        NOT NULL,
    message      TEXT        NOT NULL,
    payload_json JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_level ON event_log(level, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id            BIGSERIAL PRIMARY KEY,
    params_json   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    seed          BIGINT      NOT NULL DEFAULT 0,
    blocks        INTEGER     NOT NULL DEFAULT 0,
    profile       TEXT        NOT NULL DEFAULT 'normal',
    total_found   INTEGER     NOT NULL DEFAULT 0,
    total_net_usd NUMERIC(24, 8) NOT NULL DEFAULT 0,
    result_json   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    duration_sec  NUMERIC(12, 4) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Сид-минимум: админ создаётся кодом (пароль хэшируется), здесь только страховка
INSERT INTO users (username, password_hash, role, must_change_password)
VALUES ('admin', 'seeded-by-code', 'admin', TRUE)
ON CONFLICT (username) DO NOTHING;
