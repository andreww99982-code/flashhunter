-- FlashHunter: базовая схема SQLite (режим по умолчанию для локального запуска).
-- Соответствует моделям python/flashhunter/db/models.py (17 таблиц).
PRAGMA journal_mode = WAL;      -- параллельное чтение во время записи телеметрии
PRAGMA foreign_keys = ON;

-- 1. Пользователи панели: логин/пароль/2FA/флаг смены пароля.
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT    NOT NULL UNIQUE,
    password_hash     TEXT    NOT NULL,             -- pbkdf2/scrypt-хэш, соль внутри строки
    role              TEXT    NOT NULL DEFAULT 'admin',
    totp_secret       TEXT,                          -- секрет TOTP (пусто до включения 2FA)
    totp_enabled      INTEGER NOT NULL DEFAULT 0,    -- 0/1
    must_change_password INTEGER NOT NULL DEFAULT 1, -- требование смены пароля при первом входе
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at     TEXT
);

-- 2. Сети (EVM-чейны), в которых работает бот.
CREATE TABLE IF NOT EXISTS networks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id          INTEGER NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    native_symbol     TEXT    NOT NULL,
    rpc_http          TEXT,
    rpc_ws            TEXT,
    explorer_url      TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    median_block_ms   INTEGER NOT NULL DEFAULT 12000, -- медиана времени блока (для дедлайнов газа)
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. Токены: адрес + сеть + decimals (критично для расчёта размеров сделок).
CREATE TABLE IF NOT EXISTS tokens (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id          INTEGER NOT NULL,
    address           TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    decimals          INTEGER NOT NULL DEFAULT 18,
    is_stable         INTEGER NOT NULL DEFAULT 0,     -- стейблкоин (для stable_peg стратегии)
    price_usd         REAL,
    UNIQUE (chain_id, address)
);
CREATE INDEX IF NOT EXISTS idx_tokens_symbol ON tokens(symbol);

-- 4. Пулы: состояние ликвидности и комиссии (обновляется ингестором).
CREATE TABLE IF NOT EXISTS pools (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id          INTEGER NOT NULL,
    address           TEXT    NOT NULL,
    dex               TEXT    NOT NULL,          -- uniswap_v2/v3/curve/...
    kind              TEXT    NOT NULL,          -- constant_product/concentrated/stableswap
    token0            TEXT    NOT NULL,
    token1            TEXT    NOT NULL,
    fee_bps           INTEGER NOT NULL DEFAULT 30,
    reserve0          TEXT,                      -- строки: точность uint256
    reserve1          TEXT,
    liquidity_usd     REAL    NOT NULL DEFAULT 0,
    updated_block     INTEGER,
    updated_at        TEXT,
    UNIQUE (chain_id, address)
);
CREATE INDEX IF NOT EXISTS idx_pools_pair ON pools(chain_id, token0, token1);
CREATE INDEX IF NOT EXISTS idx_pools_liquidity ON pools(liquidity_usd DESC);

-- 5. Конфигурация стратегий (включается/выключается из UI).
CREATE TABLE IF NOT EXISTS strategy_configs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    enabled           INTEGER NOT NULL DEFAULT 1,
    chains            TEXT    NOT NULL DEFAULT '[]',  -- JSON-массив chain_id
    params_json       TEXT    NOT NULL DEFAULT '{}',  -- параметры стратегии (пороги, размеры)
    priority          INTEGER NOT NULL DEFAULT 100,
    updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 6. Найденные возможности (арбитраж, ликвидации, JIT, интенты).
CREATE TABLE IF NOT EXISTS opportunities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    uid               TEXT    NOT NULL UNIQUE,     -- идентификатор из движка (журнал/WS)
    strategy          TEXT    NOT NULL,
    chain_id          INTEGER NOT NULL,
    block_number      INTEGER,
    token_in          TEXT    NOT NULL,
    token_out         TEXT    NOT NULL,
    dex_buy           TEXT    NOT NULL,
    dex_sell          TEXT    NOT NULL,
    size_usd          REAL    NOT NULL DEFAULT 0,
    profit_usd        REAL    NOT NULL DEFAULT 0,
    net_profit_usd    REAL    NOT NULL DEFAULT 0,
    gas_usd           REAL    NOT NULL DEFAULT 0,
    flash_fee_usd     REAL    NOT NULL DEFAULT 0,
    roi_pct           REAL    NOT NULL DEFAULT 0,
    confidence        REAL    NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'found', -- found/simulated/executed/rejected/expired
    reason            TEXT,
    latency_us        INTEGER,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_opps_created ON opportunities(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opps_strategy ON opportunities(strategy, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opps_status ON opportunities(status);

-- 7. Сделки: от симуляции до включения в блок.
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_uid   TEXT,
    strategy          TEXT    NOT NULL,
    chain_id          INTEGER NOT NULL,
    status            TEXT    NOT NULL,            -- simulated/submitted/included/reverted/cancelled
    tx_hash           TEXT,
    bundle_hash       TEXT,
    relay             TEXT,
    nonce             INTEGER,
    gas_used          INTEGER,
    gas_price_gwei    REAL,
    profit_usd        REAL    NOT NULL DEFAULT 0,
    size_usd          REAL    NOT NULL DEFAULT 0,
    dry_run           INTEGER NOT NULL DEFAULT 1,
    block_number      INTEGER,
    revert_reason     TEXT,                      -- причина реверта/отказа (боевой режим)
    details           TEXT,                      -- JSON: pre-flight, ответ релея, факты из блока
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    included_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_chain ON trades(chain_id, created_at DESC);

-- 8. Кошельки: горячий/тёплый/холодный + лимиты.
CREATE TABLE IF NOT EXISTS wallets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT    NOT NULL,
    address           TEXT    NOT NULL UNIQUE,
    kind              TEXT    NOT NULL DEFAULT 'hot',  -- hot/warm/cold
    chain_id          INTEGER NOT NULL DEFAULT 1,
    balance_usd       REAL    NOT NULL DEFAULT 0,
    max_exposure_usd  REAL    NOT NULL DEFAULT 10000,
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 9. Лимиты риска (включаются/выключаются и правятся из UI).
CREATE TABLE IF NOT EXISTS risk_limits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL UNIQUE,     -- daily_loss, hourly_loss, max_exposure_chain, ...
    value_num         REAL    NOT NULL,
    unit              TEXT    NOT NULL DEFAULT 'usd',
    description       TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 10. Настройки панели (пороги, режим, kill-switch).
CREATE TABLE IF NOT EXISTS settings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key               TEXT    NOT NULL UNIQUE,
    value             TEXT    NOT NULL,
    is_secret         INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 11. API-ключи (хэш + превью), выдаются панелью для интеграций.
CREATE TABLE IF NOT EXISTS api_keys (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT    NOT NULL,
    key_preview       TEXT    NOT NULL,            -- первые/последние символы для UI
    key_hash          TEXT    NOT NULL UNIQUE,     -- хэш полного ключа
    scopes            TEXT    NOT NULL DEFAULT '["read"]',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at      TEXT
);

-- 12. Сессии панели (ревокация JWT).
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id          TEXT    NOT NULL UNIQUE,     -- jti токена
    user_agent        TEXT,
    ip                TEXT,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at        TEXT
);

-- 13. Развёрнутые контракты (адреса по сетям).
CREATE TABLE IF NOT EXISTS contracts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,            -- FlashLoanArbitrage/Liquidator/JITVault/BridgeAdapter
    chain_id          INTEGER NOT NULL,
    address           TEXT,
    deploy_tx         TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    abi_json          TEXT,
    version           TEXT,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (name, chain_id)
);

-- 14. Метрики латентности по сервисам (бюджеты ТЗ §1.2).
CREATE TABLE IF NOT EXISTS latency_metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    service           TEXT    NOT NULL,            -- ingestor/pool-indexer/sim-engine/...
    chain_id          INTEGER,
    stage             TEXT    NOT NULL,            -- block/sim/sign/submit/...
    latency_us        INTEGER NOT NULL,
    budget_us         INTEGER NOT NULL,
    within_budget     INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_latency_service ON latency_metrics(service, created_at DESC);

-- 15. Записи аудита: кто и что менял в панели.
CREATE TABLE IF NOT EXISTS audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    actor             TEXT    NOT NULL,
    action            TEXT    NOT NULL,
    target            TEXT,
    payload_json      TEXT    NOT NULL DEFAULT '{}',
    ip                TEXT,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- 16. Логи событий (структурированные, для страницы /logs).
CREATE TABLE IF NOT EXISTS event_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    level             TEXT    NOT NULL DEFAULT 'info',
    source            TEXT    NOT NULL,            -- имя модуля/сервиса
    message           TEXT    NOT NULL,
    payload_json      TEXT    NOT NULL DEFAULT '{}',
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_event_level ON event_log(level, created_at DESC);

-- 17. Прогоны бэктеста и их результаты (страница /backtest).
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    params_json       TEXT    NOT NULL DEFAULT '{}',
    seed              INTEGER NOT NULL DEFAULT 0,
    blocks            INTEGER NOT NULL DEFAULT 0,
    profile           TEXT    NOT NULL DEFAULT 'normal',
    total_found       INTEGER NOT NULL DEFAULT 0,
    total_net_usd     REAL    NOT NULL DEFAULT 0,
    result_json       TEXT    NOT NULL DEFAULT '{}',
    duration_sec      REAL    NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Минимальный сид: админ создаётся кодом (с хэшем пароля), здесь — только страховка.
INSERT OR IGNORE INTO users (username, password_hash, role, must_change_password)
VALUES ('admin', 'seeded-by-code', 'admin', 1);
