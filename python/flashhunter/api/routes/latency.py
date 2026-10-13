"""Латентность: бюджеты сервисов (ТЗ п.1.2), измеренные значения, гистограммы."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from flashhunter.api.deps import CurrentUser, SessionDep, service
from flashhunter.db.models import LatencyMetric

router = APIRouter(prefix="/latency", tags=["latency"])

# Бюджеты латентности из ТЗ (мкс) — эталон, с которым сравниваются замеры
SERVICE_BUDGETS_US: dict[str, int] = {
    "ingestor": 5_000,
    "pool-indexer": 2_000,
    "sim-engine": 8_000,
    "graph-engine": 3_000,
    "strategy-arb": 5_000,
    "strategy-liq": 5_000,
    "strategy-jit": 5_000,
    "strategy-backrun": 5_000,
    "strategy-intent": 20_000,
    "bundle-builder": 3_000,
    "relay-client": 10_000,
    "risk-engine": 1_000,
    "api": 20_000,
}


@router.get("/metrics")
async def metrics(
    user: CurrentUser,
    db: SessionDep,
    service_name: str | None = Query(default=None, alias="service"),
    limit: int = Query(default=200, le=2000),
) -> dict[str, Any]:
    """Метрики латентности из БД + бюджет; снимок живого оркестратора."""
    stmt = select(LatencyMetric).order_by(desc(LatencyMetric.ts)).limit(limit)
    if service_name:
        stmt = stmt.where(LatencyMetric.service == service_name)
    rows = (await db.execute(stmt)).scalars().all()

    # агрегат по сервисам: последние значения + среднее p99
    agg = (
        await db.execute(
            select(
                LatencyMetric.service,
                func.count(LatencyMetric.id),
                func.coalesce(func.avg(LatencyMetric.p50_us), 0),
                func.coalesce(func.avg(LatencyMetric.p95_us), 0),
                func.coalesce(func.avg(LatencyMetric.p99_us), 0),
            ).group_by(LatencyMetric.service)
        )
    ).all()

    svc = service()
    return {
        "budgets_us": SERVICE_BUDGETS_US,
        "services": [
            {
                "service": name,
                "samples": int(count),
                "p50_us": int(avg50),
                "p95_us": int(avg95),
                "p99_us": int(avg99),
                "budget_us": SERVICE_BUDGETS_US.get(name, 0),
                "within_budget": int(avg99) <= SERVICE_BUDGETS_US.get(name, 10**9),
            }
            for name, count, avg50, avg95, avg99 in agg
        ],
        "history": [
            {
                "ts": row.ts.isoformat(),
                "service": row.service,
                "metric": row.metric,
                "p50_us": row.p50_us,
                "p95_us": row.p95_us,
                "p99_us": row.p99_us,
                "count": row.count,
            }
            for row in rows
        ],
        "live": {
            "avg_block_latency_ms": svc.stats.avg_latency_ms,
            "buffer_size": len(svc.recent_opportunities),
            "risk_p99_ms": svc.risk.latency_p99_ms(),
        },
    }


@router.get("/histogram")
async def histogram(user: CurrentUser, db: SessionDep, buckets: int = Query(default=12, le=48)) -> dict[str, Any]:
    """Гистограмма задержек исполнения сделок (мс) для графика в UI."""
    rows = (
        await db.execute(select(LatencyMetric.p99_us).order_by(desc(LatencyMetric.ts)).limit(500))
    ).scalars().all()
    values = [v / 1000 for v in rows if v > 0]  # мкс -> мс
    if not values:
        return {"buckets": [], "max_ms": 0.0}
    hi = max(values)
    step = max(hi / buckets, 0.001)
    buckets_data = []
    for i in range(buckets):
        lo = i * step
        hi_edge = lo + step
        count = sum(1 for v in values if lo <= v < hi_edge)
        buckets_data.append({"from_ms": round(lo, 3), "to_ms": round(hi_edge, 3), "count": count})
    return {"buckets": buckets_data, "max_ms": round(hi, 3), "samples": len(values)}
