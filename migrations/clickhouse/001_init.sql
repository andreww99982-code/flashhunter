-- FlashHunter: ClickHouse-схема для телеметрии высокой частоты.
-- Сюда пишутся: латентность каждого этапа, отправки в релеи, тики PnL, метрики сервисов.
-- Назначение: страницы /latency, /analytics и экспорт в BI без нагрузки на Postgres.

CREATE DATABASE IF NOT EXISTS flashhunter;

-- 1. Латентность этапов: 1 строка на каждый замер (миллионы строк в сутки).
CREATE TABLE IF NOT EXISTS flashhunter.latency
(
    ts            DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    instance_id   LowCardinality(String),        -- идентификатор процесса бота (blue/green деплой)
    service       LowCardinality(String),        -- ingestor/pool-indexer/sim-engine/...
    stage         LowCardinality(String),        -- block/sim/sign/submit/inclusion
    chain_id      UInt32,
    block_number  UInt64,
    latency_us    UInt32,
    budget_us     UInt32,
    within_budget UInt8,
    strategy      LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (service, stage, ts)
TTL ts + INTERVAL 180 DAY DELETE
SETTINGS index_granularity = 8192;

-- 2. Отправки в релеи: чем заканчивалась каждая попытка.
CREATE TABLE IF NOT EXISTS flashhunter.relay_submissions
(
    ts          DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    instance_id LowCardinality(String),
    relay       LowCardinality(String),
    chain_id    UInt32,
    bundle_hash String,
    tx_hash     String,
    accepted    UInt8,
    latency_ms  Float32,
    error       String CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (relay, ts)
TTL ts + INTERVAL 180 DAY DELETE;

-- 3. PnL-поток: инкрементальные изменения капитала (для графика реального времени).
CREATE TABLE IF NOT EXISTS flashhunter.pnl_ticks
(
    ts          DateTime64(3, 'UTC'),
    instance_id LowCardinality(String),
    chain_id    UInt32,
    wallet      String,
    strategy    LowCardinality(String),
    equity_usd  Decimal(24, 8),
    delta_usd   Decimal(24, 8),
    gas_usd     Decimal(18, 8)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (chain_id, strategy, ts)
TTL ts + INTERVAL 365 DAY DELETE;

-- 4. Снимки здоровья сервисов: uptime, глубина индекса, задержка головы сети.
CREATE TABLE IF NOT EXISTS flashhunter.service_health
(
    ts           DateTime64(3, 'UTC'),
    instance_id  LowCardinality(String),
    service      LowCardinality(String),
    up           UInt8,
    head_lag_ms  UInt32,      -- отставание от головы сети
    pools_indexed UInt32,
    queue_depth  UInt32,
    last_error   String
)
ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMM(ts)
ORDER BY (instance_id, service, ts)
TTL ts + INTERVAL 90 DAY DELETE;

-- 5. Материализованное представление: минутная агрегация латентности.
CREATE MATERIALIZED VIEW IF NOT EXISTS flashhunter.latency_1m
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (service, stage, minute)
AS SELECT
    toStartOfMinute(ts)                AS minute,
    service,
    stage,
    count()                            AS samples,
    quantileState(0.5)(latency_us)     AS p50_state,
    quantileState(0.95)(latency_us)    AS p95_state,
    quantileState(0.99)(latency_us)    AS p99_state,
    avg(within_budget)                 AS within_budget_avg
FROM flashhunter.latency
GROUP BY minute, service, stage;

-- 6. Хелпер-представление «свежие данные» (последние 5 минут) для дашборда.
CREATE VIEW IF NOT EXISTS flashhunter.latency_recent AS
SELECT * FROM flashhunter.latency WHERE ts >= now() - INTERVAL 5 MINUTE;
