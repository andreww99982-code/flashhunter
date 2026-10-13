"""Логи: поток событий оркестратора (live) и журнал аудита действий оператора."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from flashhunter.api.deps import CurrentUser, Page, SessionDep, service
from flashhunter.db.models import AuditLog

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/events")
async def events(user: CurrentUser, limit: int = Query(default=100, le=500)) -> dict[str, Any]:
    """Последние события оркестратора (opportunity/execution/kill_switch/sweep/mode)."""
    svc = service()
    items = svc.recent_events(limit)
    items.reverse()
    return {"items": items, "count": len(items)}


@router.get("/audit")
async def audit(user: CurrentUser, db: SessionDep, page: Page) -> dict[str, Any]:
    """Журнал аудита: кто и что менял (вход, режимы, лимиты, ручные сделки)."""
    rows = (
        await db.execute(select(AuditLog).order_by(desc(AuditLog.ts)).offset(page.offset).limit(page.limit))
    ).scalars().all()
    return {
        "items": [
            {"id": r.id, "ts": r.ts.isoformat(), "username": r.username, "action": r.action,
             "payload": r.payload, "ip": r.ip}
            for r in rows
        ]
    }
