"""FastAPI-приложение FlashHunter: REST /api/v1, WebSocket /ws, метрики, SPA-раздача UI.

Точка входа: ``python -m uvicorn flashhunter.api.app:app --app-dir python --port 8000``
или консольная команда ``flashhunter-api``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from flashhunter import __version__
from flashhunter.api import metrics as m
from flashhunter.api.routes import (
    analytics,
    auth,
    backtest,
    contracts,
    dashboard,
    history,
    latency,
    logs,
    opportunities,
    settings as settings_routes,
    strategies,
    wallets,
)
from flashhunter.api.ws import router as ws_router
from flashhunter.config.settings import get_settings
from flashhunter.db.seed import seed_all
from flashhunter.db.session import dispose_engine, init_models, session_scope
from flashhunter.strategies.service import get_strategy_service

log = logging.getLogger(__name__)
settings = get_settings()

API_PREFIX = "/api/v1"


def _ui_dist() -> Path:
    """Каталог собранного UI (Vite outDir)."""
    candidates = [
        # 1) сборка внутри Python-пакета (npm run build: outDir=../python/flashhunter/ui_static)
        Path(__file__).resolve().parents[1] / "ui_static",
        # 2) сборка в монорепозитории
        Path(__file__).resolve().parents[3] / "ui" / "dist",
        Path.cwd() / "ui" / "dist",
        # 3) docker-образ
        Path("/app/ui/dist"),
        Path("/app/python/flashhunter/ui_static"),
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return candidates[0]


async def _heartbeat_loop() -> None:
    """Раз в 5 секунд пишет пульс оркестратора и метрики в Prometheus."""
    svc = get_strategy_service()
    while True:
        try:
            await svc.heartbeat()
            from flashhunter.pnl.tracker import PnlTracker

            async with session_scope() as session:
                tracker = PnlTracker(session)
                for period in ("24h", "7d", "30d"):
                    summary = await tracker.summary(period)
                    m.pnl_usd.labels(period=period).set(summary.net_usd)
            state = svc.state()
            m.service_up.set(1 if state["running"] else 0)
            m.kill_switch_active.set(1 if state["kill_switch"] else 0)
            m.dry_run_active.set(1 if state["dry_run"] else 0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фоновый цикл не должен падать
            log.exception("heartbeat failed")
        await asyncio.sleep(5)


def _open_browser_later(url: str, delay: float = 2.0) -> None:
    """Открывает браузер через delay секунд (FLASHHUNTER_AUTO_OPEN_BROWSER=true)."""

    def _open() -> None:
        time.sleep(delay)
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    threading.Thread(target=_open, daemon=True, name="auto-open-browser").start()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Старт: схема БД, сид, запуск оркестратора, фоновые задачи, автооткрытие браузера."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("FlashHunter %s starting (env=%s, dry_run=%s, db=%s)",
             __version__, settings.env, settings.dry_run, settings.db_mode)

    await init_models()
    async with session_scope() as session:
        created = await seed_all(session)
        log.info("seed: %s", created)

    svc = get_strategy_service()
    if os.environ.get("FLASHHUNTER_DISABLE_ORCHESTRATOR", "").lower() not in {"1", "true", "yes"}:
        await svc.start()

    tasks = [asyncio.create_task(_heartbeat_loop(), name="heartbeat")]
    url = f"http://localhost:{settings.api_port}"
    if settings.auto_open_browser:
        _open_browser_later(url)
        log.info("UI будет открыт в браузере: %s", url)

    yield

    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await svc.stop()
    await dispose_engine()
    log.info("FlashHunter stopped")


app = FastAPI(
    title="FlashHunter API",
    version=__version__,
    description="Промышленный DeFi-комбайн: flash-loan арбитраж, ликвидации, JIT, backrun, intents",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Роутеры API -----------------------------------------------------------
for router_module in (
    auth,
    dashboard,
    opportunities,
    strategies,
    contracts,
    wallets,
    history,
    analytics,
    latency,
    settings_routes,
    logs,
    backtest,
):
    app.include_router(router_module.router, prefix=API_PREFIX)

app.include_router(ws_router)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Добавляет X-Process-Time ко всем ответам (контроль бюджета API < 20 мс)."""
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
    return response


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Экспорт метрик Prometheus (скрейпится Prometheus-сервером из infra/)."""
    return Response(generate_latest(m.REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get(f"{API_PREFIX}/ping")
async def ping() -> dict[str, object]:
    """Лёгкий ping для health-check контейнера: без обращения к БД и оркестратору.

    Используется Docker HEALTHCHECK, k8s-пробами и скриптом bootstrap —
    поэтому обязан отвечать за микросекунды и не зависеть от состояния БД.
    """
    return {"pong": True, "version": __version__, "env": settings.env, "dry_run": settings.dry_run}


@app.get(f"{API_PREFIX}/health")
async def health() -> JSONResponse:
    """Health-check: статус БД, оркестратора, режима работы."""
    svc = get_strategy_service()
    state = svc.state()
    return JSONResponse(
        {
            "status": "ok" if state["running"] else "degraded",
            "version": __version__,
            "env": settings.env,
            "dry_run": state["dry_run"],
            "kill_switch": state["kill_switch"],
            "blocks_processed": state["stats"]["blocks_processed"],
            "uptime_sec": state["stats"]["uptime_sec"],
            # Боевое состояние: откуда данные, чем подписываем, куда отправляем.
            # Режим readonly без ключа виден сразу — оператор не должен гадать, идут ли сделки.
            "market_source": state["execution"]["market"],
            "execution_mode": state["execution"]["mode"],
            "signer_mode": state["execution"]["signer_mode"],
            "signer": state["execution"]["signer"],
            "executor_contract": state["execution"]["executor_contract"],
            "relays": state["execution"]["relays"],
            "ready_to_submit": bool(
                state["execution"]["signer_mode"] == "signer" and state["execution"]["executor_contract"]
            ),
        }
    )


@app.get(f"{API_PREFIX}/config/public")
async def public_config() -> dict[str, object]:
    """Публичная часть конфигурации для UI (без секретов) — доступна без авторизации."""
    return {
        "version": __version__,
        "env": settings.env,
        "dry_run": settings.dry_run,
        "force_password_change": settings.force_password_change,
        "chains": [c.name for c in settings.chains()] or ["ethereum"],
        "mock_market_data": settings.mock_market_data,
        "relays": list(settings.live_relays()) or ["flashbots"],
        "ui_available": (_ui_dist() / "index.html").exists(),
    }


# --- Раздача UI (SPA) ------------------------------------------------------
_UI_DIR = _ui_dist()
if (_UI_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_UI_DIR / "assets")), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str) -> Response:
    """SPA-fallback: неизвестный путь отдаёт index.html (маршрутизация React Router).

    Важно: пути API и служебные эндпоинты НЕ должны попадать сюда — иначе клиент
    получит HTML вместо JSON-ошибки и решит, что запрос удался. Для них честный 404.
    """
    reserved = ("api/", "ws", "metrics", "favicon")
    if full_path.startswith(reserved):
        return JSONResponse({"detail": "not found", "path": f"/{full_path}"}, status_code=404)

    index = _UI_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse(
        """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>FlashHunter API</title></head>
<body style="background:#0a0e27;color:#e8ecff;font-family:Inter,system-ui,sans-serif;padding:40px">
<h1>FlashHunter API работает</h1>
<p>Собранный UI не найден. Соберите фронтенд: <code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<ul>
  <li><a style="color:#7c5cff" href="/api/docs">OpenAPI (Swagger)</a></li>
  <li><a style="color:#7c5cff" href="/api/v1/health">Health</a></li>
  <li><a style="color:#7c5cff" href="/metrics">Prometheus</a></li>
</ul>
</body></html>""",
        status_code=200,
    )


def main() -> None:
    """Консольная точка входа: запуск uvicorn с параметрами из настроек."""
    import uvicorn

    uvicorn.run(
        "flashhunter.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        workers=1,  # оркестратор в процессе API; для масштабирования — отдельный процесс
    )


if __name__ == "__main__":  # pragma: no cover
    main()
