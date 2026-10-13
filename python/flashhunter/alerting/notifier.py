"""Алертинг FlashHunter.

Бизнес-задача: оператор должен узнать о критических событиях (kill-switch, серия
ревертов, потеря связи с RPC, депозит ниже газового порога) быстрее, чем это
сделает рынок. Канал — Telegram/Discord/webhook; в dry-run уведомления пишутся
в лог, чтобы не спамить боевой чат.

Каналы настраиваются через .env:
    ALERT_TELEGRAM_TOKEN / ALERT_TELEGRAM_CHAT_ID
    ALERT_DISCORD_WEBHOOK
    ALERT_WEBHOOK_URL
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from flashhunter.config.settings import get_settings

log = logging.getLogger(__name__)


class AlertLevel(str, enum.Enum):
    """Уровень критичности уведомления (влияет на эмодзи и дедупликацию)."""

    info = "info"
    warning = "warning"
    critical = "critical"


_ICONS: dict[AlertLevel, str] = {
    AlertLevel.info: "ℹ️",
    AlertLevel.warning: "⚠️",
    AlertLevel.critical: "🚨",
}


@dataclass(slots=True)
class Alert:
    """Уведомление: тема, текст, уровень и произвольные данные для разбора."""

    title: str
    message: str
    level: AlertLevel = AlertLevel.info
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.utcnow)

    def as_text(self) -> str:
        """Текст для мессенджера: иконка, заголовок, сообщение и время."""
        return (
            f"{_ICONS[self.level]} <b>{self.title}</b>\n"
            f"{self.message}\n"
            f"<i>FlashHunter · {self.ts.strftime('%H:%M:%S')} UTC</i>"
        )


class AlertNotifier:
    """Отправка уведомлений с дедупликацией и rate-limit (защита от лавины алертов)."""

    def __init__(self, min_interval_sec: float = 30.0) -> None:
        self.settings = get_settings()
        self.min_interval_sec = min_interval_sec
        self.sent: list[Alert] = []
        self._last_sent: dict[str, datetime] = {}
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    async def send(self, alert: Alert) -> bool:
        """Отправляет уведомление во все настроенные каналы (best-effort, без падений).

        Возвращает True, если хотя бы один канал принял сообщение.
        """
        if not self._should_send(alert):
            log.debug("alert suppressed by dedup: %s", alert.title)
            return False

        self.sent.append(alert)
        self._last_sent[alert.title] = alert.ts
        delivered = False

        tasks = []
        if self.settings.alert_telegram_token_str and self.settings.alert_telegram_chat_id:
            tasks.append(self._send_telegram(alert))
        if self.settings.alert_discord_webhook:
            tasks.append(self._send_discord(alert))
        if self.settings.alert_webhook_url:
            tasks.append(self._send_webhook(alert))

        if not tasks:
            # Каналы не настроены (частый случай для локального запуска) — пишем в лог.
            log.warning("ALERT [%s] %s — %s", alert.level.value, alert.title, alert.message)
            return True

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("alert delivery failed: %s", result)
            elif result:
                delivered = True
        return delivered

    async def notify_critical(self, title: str, message: str, **payload: Any) -> bool:
        """Короткий путь для критических событий (kill-switch, revert-шторм)."""
        return await self.send(Alert(title=title, message=message, level=AlertLevel.critical, payload=payload))

    async def notify_warning(self, title: str, message: str, **payload: Any) -> bool:
        """Предупреждение: газ выше обычного, задержка релея, падение win-rate."""
        return await self.send(Alert(title=title, message=message, level=AlertLevel.warning, payload=payload))

    # ------------------------------------------------------------------ #
    def _should_send(self, alert: Alert) -> bool:
        """Дедупликация: одинаковые заголовки не чаще min_interval_sec; critical — всегда."""
        if alert.level is AlertLevel.critical:
            return True
        last = self._last_sent.get(alert.title)
        if last is None:
            return True
        return datetime.utcnow() - last >= timedelta(seconds=self.min_interval_sec)

    async def _http(self) -> httpx.AsyncClient:
        """Ленивый httpx-клиент с таймаутом (не блокирует торговый цикл)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.alert_timeout_ms / 1000)
        return self._client

    async def _send_telegram(self, alert: Alert) -> bool:
        """Telegram Bot API sendMessage (parse_mode=HTML)."""
        client = await self._http()
        url = f"https://api.telegram.org/bot{self.settings.alert_telegram_token_str}/sendMessage"
        response = await client.post(
            url,
            json={
                "chat_id": self.settings.alert_telegram_chat_id,
                "text": alert.as_text(),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return response.status_code == 200

    async def _send_discord(self, alert: Alert) -> bool:
        """Discord webhook (content + embed-цвет по уровню)."""
        client = await self._http()
        colors = {AlertLevel.info: 0x4DC9FF, AlertLevel.warning: 0xFFB547, AlertLevel.critical: 0xFF4D6D}
        response = await client.post(
            self.settings.alert_discord_webhook,
            json={
                "content": f"**{alert.title}**\n{alert.message}",
                "embeds": [{"title": alert.title, "description": alert.message, "color": colors[alert.level]}],
            },
        )
        return response.status_code in {200, 204}

    async def _send_webhook(self, alert: Alert) -> bool:
        """Универсальный JSON-webhook (PagerDuty, n8n, собственный сервис)."""
        client = await self._http()
        response = await client.post(
            self.settings.alert_webhook_url,
            json={
                "title": alert.title,
                "message": alert.message,
                "level": alert.level.value,
                "payload": alert.payload,
                "ts": alert.ts.isoformat(),
                "source": "flashhunter",
            },
        )
        return response.status_code < 400

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент (вызывается при остановке приложения)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_notifier: AlertNotifier | None = None


def get_notifier() -> AlertNotifier:
    """Глобальный нотификатор (используется риск-движком и API)."""
    global _notifier
    if _notifier is None:
        _notifier = AlertNotifier()
    return _notifier


__all__ = ["Alert", "AlertLevel", "AlertNotifier", "get_notifier"]
