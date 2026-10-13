"""Общие зависимости FastAPI: доступ к сервисам, пагинация, аудит действий."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from flashhunter.api.auth import get_current_user, require_password_changed, require_totp_if_enabled
from flashhunter.db.models import AuditLog, User
from flashhunter.db.session import get_db
from flashhunter.strategies.service import get_strategy_service


def service() -> Any:
    """Синглтон StrategyService (оркестратор)."""
    return get_strategy_service()


@dataclass(slots=True)
class Pagination:
    """Параметры пагинации для списочных ручек."""

    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    """Стандартная пагинация limit/offset."""
    return Pagination(limit=limit, offset=offset)


async def write_audit(
    db: AsyncSession, request: Request, username: str, action: str, payload: dict[str, Any]
) -> None:
    """Пишет действие в audit_log (критические операции)."""
    from flashhunter.api.auth import client_ip

    db.add(AuditLog(username=username, action=action, payload=payload, ip=client_ip(request)))
    await db.flush()


# До смены пароля доступны только /auth/* — там используется UserDep (без этого guard-а)
UserDep = Annotated[User, Depends(get_current_user)]
CurrentUser = Annotated[User, Depends(require_password_changed)]
CriticalUser = Annotated[User, Depends(require_totp_if_enabled)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]
Page = Annotated[Pagination, Depends(pagination)]
