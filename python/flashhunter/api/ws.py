"""WebSocket: поток событий (возможности, сделки, логи сервисов) и снапшоты состояния.

Протокол:
    клиент -> {"type":"subscribe","channels":["events","state"]}
    сервер -> {"type":"opportunity"|"execution"|"state"|"log"|"pong", "data":{...}}
Аутентификация: ?token=<JWT> либо cookie fh_session (проверяется при подключении).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from flashhunter.api.auth import decode_access_token
from flashhunter.strategies.service import get_strategy_service

log = logging.getLogger(__name__)
router = APIRouter()


def _authenticate(token: str | None) -> bool:
    """Проверяет JWT из query-параметра (WS не умеет отправлять заголовки из браузера)."""
    if not token:
        return False
    try:
        decode_access_token(token)
    except Exception:  # noqa: BLE001 — любая проблема = отказ
        return False
    return True


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Основной WS: события оркестратора + периодический снапшот состояния."""
    cookie_token = websocket.cookies.get("fh_session")
    if not (_authenticate(token) or _authenticate(cookie_token)):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    svc = get_strategy_service()
    queue = svc.subscribe()
    log.info("ws client connected (subscribers=%d)", len(svc._subscribers))

    async def sender() -> None:
        """Пересылает события подписчику."""
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    async def snapshots() -> None:
        """Раз в секунду отправляет снапшот состояния (дашборд, метрики сервисов)."""
        while True:
            await asyncio.sleep(1.0)
            await websocket.send_json({"type": "state", "data": svc.state()})

    sender_task = asyncio.create_task(sender())
    snapshot_task = asyncio.create_task(snapshots())
    try:
        while True:
            message = await websocket.receive_json()
            mtype = message.get("type")
            if mtype == "ping":
                await websocket.send_json({"type": "pong", "data": {}})
            elif mtype == "pause":
                svc.pause()
                await websocket.send_json({"type": "log", "data": {"level": "warning", "message": "orchestrator paused"}})
            elif mtype == "resume":
                svc.resume()
                await websocket.send_json({"type": "log", "data": {"level": "info", "message": "orchestrator resumed"}})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws error: %s", exc)
    finally:
        for task in (sender_task, snapshot_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        svc.unsubscribe(queue)
        log.info("ws client disconnected")


@router.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Лог-поток: только события (без снапшотов состояния) — страница /logs."""
    cookie_token = websocket.cookies.get("fh_session")
    if not (_authenticate(token) or _authenticate(cookie_token)):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    svc = get_strategy_service()
    queue = svc.subscribe()
    try:
        for event in svc.recent_events(50):
            await websocket.send_json({"type": "log", "data": event})
        while True:
            event = await queue.get()
            await websocket.send_json({"type": "log", "data": event})
    except WebSocketDisconnect:
        pass
    finally:
        svc.unsubscribe(queue)
