"""История сделок: список, детали, экспорт CSV/JSON (Parquet — в cluster-режиме)."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select

from flashhunter.api.deps import CurrentUser, Page, SessionDep
from flashhunter.db.models import Trade

router = APIRouter(prefix="/history", tags=["history"])

EXPORT_COLUMNS = [
    "id", "ts", "chain_id", "strategy", "tx_hash", "bundle_hash", "relay",
    "block_number", "gas_used", "gas_price_gwei", "priority_fee_gwei",
    "actual_profit_usd", "status", "revert_reason", "latency_ms",
]


def _trade_row(trade: Trade) -> dict[str, Any]:
    """Сериализация сделки для API/экспорта."""
    return {
        "id": trade.id,
        "ts": trade.ts.isoformat(),
        "chain_id": trade.chain_id,
        "strategy": trade.strategy,
        "tx_hash": trade.tx_hash,
        "bundle_hash": trade.bundle_hash,
        "relay": trade.relay,
        "block_number": trade.block_number,
        "gas_used": trade.gas_used,
        "gas_price_gwei": trade.gas_price_gwei,
        "priority_fee_gwei": trade.priority_fee_gwei,
        "actual_profit_usd": float(trade.actual_profit_usd),
        "status": trade.status.value,
        "revert_reason": trade.revert_reason,
        "latency_ms": trade.latency_ms,
    }


@router.get("")
async def list_trades(
    user: CurrentUser,
    db: SessionDep,
    page: Page,
    strategy: str | None = Query(default=None),
    chain_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    since: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Постраничная история сделок с фильтрами (UI: /history)."""
    stmt = select(Trade)
    if strategy:
        stmt = stmt.where(Trade.strategy == strategy)
    if chain_id:
        stmt = stmt.where(Trade.chain_id == chain_id)
    if status_filter:
        stmt = stmt.where(Trade.status == status_filter)
    if since:
        stmt = stmt.where(Trade.ts >= since)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(desc(Trade.ts)).offset(page.offset).limit(page.limit))).scalars().all()
    return {"items": [_trade_row(t) for t in rows], "total": int(total or 0)}


@router.get("/{trade_id}")
async def trade_detail(trade_id: int, user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Детали одной сделки (включая причину revert для разбора инцидентов)."""
    trade = await db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trade not found")
    return _trade_row(trade)


@router.get("/export/csv")
async def export_csv(
    user: CurrentUser,
    db: SessionDep,
    strategy: str | None = Query(default=None),
    chain_id: int | None = Query(default=None),
    limit: int = Query(default=10_000, le=100_000),
) -> StreamingResponse:
    """Экспорт истории в CSV (Excel-совместимый, UTF-8 BOM для корректной кириллицы)."""
    stmt = select(Trade).order_by(desc(Trade.ts)).limit(limit)
    if strategy:
        stmt = stmt.where(Trade.strategy == strategy)
    if chain_id:
        stmt = stmt.where(Trade.chain_id == chain_id)
    rows = (await db.execute(stmt)).scalars().all()

    buffer = io.StringIO()
    buffer.write("\ufeff")  # BOM для Excel
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for trade in rows:
        writer.writerow(_trade_row(trade))
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="flashhunter-trades.csv"'},
    )
