"""Асинхронный доступ к БД: engine, фабрика сессий, инициализация схемы.

Режимы:
    * ``sqlite`` (single-node)  — aiosqlite, WAL, схема создаётся автоматически;
    * ``postgres`` (cluster)    — asyncpg, схема накатывается Alembic/миграциями,
      но ``init_models`` также умеет создать её для быстрого старта.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from flashhunter.config.settings import get_settings
from flashhunter.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Ленивый синглтон async-engine (SQLite: WAL + busy_timeout для параллельных писателей)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, object] = {"echo": False, "future": True}
        if url.startswith("sqlite"):
            kwargs.update(connect_args={"timeout": 30}, pool_pre_ping=True)
        else:
            kwargs.update(pool_size=20, max_overflow=10, pool_pre_ping=True)
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий (expire_on_commit=False — объекты живут после commit)."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер: сессия с автоматическим commit/rollback."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость ``Depends(get_db)``."""
    async with session_scope() as session:
        yield session


async def init_models() -> None:
    """Создание схемы при старте (idempotent). В cluster-режиме применяются миграции."""
    engine = get_engine()
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            from sqlalchemy import text

            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Корректное закрытие пула соединений (graceful shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
