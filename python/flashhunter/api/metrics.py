"""Prometheus-метрики: латентность, найденные возможности, сделки, PnL, статус сервисов."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

opportunities_total = Counter(
    "flashhunter_opportunities_total",
    "Найденные возможности по стратегиям и сетям",
    ["strategy", "chain_id"],
    registry=REGISTRY,
)

executions_total = Counter(
    "flashhunter_executions_total",
    "Исполненные/отклонённые сделки",
    ["strategy", "status"],
    registry=REGISTRY,
)

pnl_usd = Gauge(
    "flashhunter_pnl_usd",
    "Накопленный PnL в USD",
    ["period"],
    registry=REGISTRY,
)

block_latency = Histogram(
    "flashhunter_block_latency_seconds",
    "Задержка обработки блока (блок -> bundle отправлен)",
    buckets=(0.005, 0.01, 0.02, 0.04, 0.08, 0.12, 0.25, 0.5, 1.0, 2.0),
    registry=REGISTRY,
)

simulation_latency = Histogram(
    "flashhunter_simulation_latency_seconds",
    "Задержка симуляции кандидата",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.008, 0.02, 0.05),
    registry=REGISTRY,
)

relay_submissions = Counter(
    "flashhunter_relay_submissions_total",
    "Отправки bundle по релеям и результатам",
    ["relay", "result"],
    registry=REGISTRY,
)

relay_latency = Histogram(
    "flashhunter_relay_latency_seconds",
    "Задержка отправки bundle в релей",
    ["relay"],
    buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0),
    registry=REGISTRY,
)

kill_switch_active = Gauge(
    "flashhunter_kill_switch_active",
    "1, если kill-switch взведён",
    registry=REGISTRY,
)

dry_run_active = Gauge(
    "flashhunter_dry_run_active",
    "1, если включён dry-run",
    registry=REGISTRY,
)

service_up = Gauge(
    "flashhunter_service_up",
    "1, если оркестратор работает",
    registry=REGISTRY,
)

gas_spend_usd = Counter(
    "flashhunter_gas_spend_usd_total",
    "Потрачено на газ (USD)",
    ["chain_id"],
    registry=REGISTRY,
)
