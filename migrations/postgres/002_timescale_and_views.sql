-- FlashHunter: представления аналитики и ретеншн для PostgreSQL.
-- Эти вьюхи — источник данных для страниц /analytics, /latency и /history
-- при FLASHHUNTER_DB_MODE=postgres (в SQLite-режиме агрегация считается в Python).

-- 1. PnL по дням и стратегиям: база для графика «прибыль во времени».
CREATE OR REPLACE VIEW v_pnl_daily AS
SELECT
    date_trunc('day', t.created_at) AS day,
    t.strategy,
    t.chain_id,
    count(*) FILTER (WHERE t.status = 'included')          AS trades_included,
    count(*) FILTER (WHERE t.status = 'reverted')          AS trades_reverted,
    coalesce(sum(t.profit_usd) FILTER (WHERE t.status = 'included'), 0) AS net_usd,
    coalesce(avg(t.size_usd), 0)                           AS avg_size_usd,
    coalesce(sum(t.gas_used), 0)                           AS total_gas
FROM trades t
GROUP BY 1, 2, 3;

-- 2. Латентность по сервисам: p50/p95/p99 против бюджета ТЗ §1.2.
CREATE OR REPLACE VIEW v_latency_by_service AS
SELECT
    service,
    date_trunc('hour', created_at) AS hour,
    count(*)                                                           AS samples,
    percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_us)            AS p50_us,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_us)            AS p95_us,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_us)            AS p99_us,
    avg(budget_us)                                                      AS budget_us,
    avg(CASE WHEN within_budget THEN 1.0 ELSE 0.0 END) * 100            AS within_budget_pct
FROM latency_metrics
GROUP BY 1, 2;

-- 3. Эффективность стратегий: конверсия найденных возможностей в сделки.
CREATE OR REPLACE VIEW v_strategy_efficiency AS
SELECT
    o.strategy,
    o.chain_id,
    count(*)                                                       AS found,
    count(*) FILTER (WHERE o.status = 'executed')                  AS executed,
    count(*) FILTER (WHERE o.status = 'rejected')                  AS rejected,
    round(100.0 * count(*) FILTER (WHERE o.status = 'executed')
          / greatest(count(*), 1), 2)                              AS conversion_pct,
    coalesce(sum(o.net_profit_usd) FILTER (WHERE o.status = 'executed'), 0) AS net_usd,
    coalesce(avg(o.roi_pct) FILTER (WHERE o.status = 'executed'), 0)        AS avg_roi_pct
FROM opportunities o
GROUP BY 1, 2;

-- 4. Конверсия релеев: какой релей реально включает наши bundle.
CREATE OR REPLACE VIEW v_relay_performance AS
SELECT
    relay,
    count(*)                                                    AS submitted,
    count(*) FILTER (WHERE status = 'included')                  AS included,
    count(*) FILTER (WHERE status = 'reverted')                  AS reverted,
    round(100.0 * count(*) FILTER (WHERE status = 'included')
          / greatest(count(*), 1), 2)                            AS inclusion_pct,
    coalesce(sum(profit_usd) FILTER (WHERE status = 'included'), 0) AS net_usd
FROM trades
WHERE relay IS NOT NULL
GROUP BY 1;

-- 5. Риск-панель: расход лимитов за сутки (kill-switch смотрит сюда).
CREATE OR REPLACE VIEW v_risk_state AS
SELECT
    'daily_loss' AS code,
    coalesce(sum(-profit_usd) FILTER (WHERE profit_usd < 0), 0) AS used_usd,
    (SELECT value_num FROM risk_limits WHERE code = 'daily_loss') AS limit_usd
FROM trades
WHERE created_at >= date_trunc('day', now()) AND status = 'included'
UNION ALL
SELECT
    'hourly_loss' AS code,
    coalesce(sum(-profit_usd) FILTER (WHERE profit_usd < 0), 0) AS used_usd,
    (SELECT value_num FROM risk_limits WHERE code = 'hourly_loss') AS limit_usd
FROM trades
WHERE created_at >= now() - interval '1 hour' AND status = 'included';

-- 6. Ретеншн: высокочастотные таблицы живут 90 дней (сырые данные), агрегаты — вечно.
CREATE OR REPLACE FUNCTION fh_retention(days integer DEFAULT 90) RETURNS void AS $$
BEGIN
    DELETE FROM latency_metrics WHERE created_at < now() - make_interval(days => days);
    DELETE FROM event_log       WHERE created_at < now() - make_interval(days => days);
    DELETE FROM audit_log       WHERE created_at < now() - make_interval(days => days * 4);
    DELETE FROM opportunities   WHERE created_at < now() - make_interval(days => days * 2)
        AND status IN ('rejected', 'expired');
END;
$$ LANGUAGE plpgsql;

-- 7. Хелпер создания месячных партиций (вызывается cron-ом раз в месяц).
CREATE OR REPLACE FUNCTION fh_create_partition(target date) RETURNS void AS $$
DECLARE
    part_name text := 'opportunities_' || to_char(target, 'YYYY_MM');
    next_date date := (date_trunc('month', target) + interval '1 month')::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF opportunities FOR VALUES FROM (%L) TO (%L)',
        part_name, date_trunc('month', target), next_date
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF trades FOR VALUES FROM (%L) TO (%L)',
        'trades_' || to_char(target, 'YYYY_MM'), date_trunc('month', target), next_date
    );
END;
$$ LANGUAGE plpgsql;

-- Текущий и следующие два месяца создаём сразу при миграции.
SELECT fh_create_partition(date_trunc('month', now())::date);
SELECT fh_create_partition((date_trunc('month', now()) + interval '1 month')::date);
SELECT fh_create_partition((date_trunc('month', now()) + interval '2 month')::date);
