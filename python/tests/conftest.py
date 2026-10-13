"""Общие фикстуры тестов FlashHunter.

ВАЖНО: переменные окружения выставляются ДО импорта пакета flashhunter,
иначе Settings закеширует значения по умолчанию из docker-профиля.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Изолированное окружение: своя SQLite-БД, без оркестратора, без автооткрытия браузера.
_TMP = Path(tempfile.gettempdir())
os.environ.setdefault("SQLITE_PATH", str(_TMP / f"fh_test_{uuid.uuid4().hex[:8]}.db"))
os.environ["FLASHHUNTER_DISABLE_ORCHESTRATOR"] = "1"
os.environ["FLASHHUNTER_AUTO_OPEN_BROWSER"] = "false"
# Имя переменной совпадает с алиасом поля Settings (MOCK_MARKET_DATA): префикс
# FLASHHUNTER_ для этого поля не поддерживается, и ошибка имени молча игнорируется
# (extra="ignore") — из-за этого тесты однажды пошли в живую сеть.
os.environ["MOCK_MARKET_DATA"] = "true"
os.environ["FLASHHUNTER_MOCK_MARKET_DATA"] = "true"
os.environ["FLASHHUNTER_FORCE_PASSWORD_CHANGE"] = "false"
os.environ["FLASHHUNTER_LOG_LEVEL"] = "WARNING"

from flashhunter.db.session import init_models  # noqa: E402
from flashhunter.db.seed import seed_all  # noqa: E402
from flashhunter.market.mock import MockMarketSource  # noqa: E402
from flashhunter.strategies.base import BlockCtx  # noqa: E402
from flashhunter.strategies.math.adapters import register_decimals  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
async def db_ready() -> AsyncIterator[None]:
    """Создаёт схему и наполняет её сид-данными."""
    await init_models()
    await seed_all()
    yield


@pytest.fixture()
def market() -> MockMarketSource:
    """Детерминированный источник рынка (нормальный профиль)."""
    return MockMarketSource(seed=42, profile="normal")


@pytest.fixture()
def volatile_market() -> MockMarketSource:
    return MockMarketSource(seed=11, profile="volatile")


def make_ctx(snapshot, chain_id: int | None = None) -> BlockCtx:
    """Собирает BlockCtx так же, как это делает StrategyService."""
    register_decimals(snapshot)
    return BlockCtx(
        chain_id=chain_id if chain_id is not None else snapshot.chain_id,
        block_number=snapshot.block_number,
        timestamp=snapshot.timestamp,
        base_fee_gwei=snapshot.base_fee_gwei,
        snapshot=snapshot,
    )


@pytest.fixture()
def api_client(db_ready: None):  # noqa: ARG001 — нужен порядок инициализации БД
    """TestClient собранного FastAPI-приложения с авторизованным админом."""
    from fastapi.testclient import TestClient

    from flashhunter.api.app import app

    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client
