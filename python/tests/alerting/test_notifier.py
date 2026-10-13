"""Тесты алертинга: каналы, дедупликация, уровни."""

from __future__ import annotations

import pytest

from flashhunter.alerting.notifier import Alert, AlertLevel, AlertNotifier


def test_alert_text_contains_title_and_level_icon() -> None:
    """Текст уведомления содержит иконку уровня, заголовок и сообщение."""
    alert = Alert(title="KILL-SWITCH", message="дневной лимит убытка превышен", level=AlertLevel.critical)
    text = alert.as_text()
    assert "🚨" in text
    assert "KILL-SWITCH" in text
    assert "дневной лимит" in text


async def test_notifier_without_channels_logs(monkeypatch) -> None:
    """Без настроенных каналов уведомление не теряется: пишем в лог и считаем доставленным."""
    notifier = AlertNotifier()
    # обнуляем все каналы
    from pydantic import SecretStr

    monkeypatch.setattr(notifier.settings, "alert_telegram_token", SecretStr(""))
    monkeypatch.setattr(notifier.settings, "alert_telegram_chat_id", "")
    monkeypatch.setattr(notifier.settings, "alert_discord_webhook", "")
    monkeypatch.setattr(notifier.settings, "alert_webhook_url", "")
    monkeypatch.setattr(notifier.settings, "alert_timeout_ms", 5000)

    delivered = await notifier.send(Alert(title="test", message="каналов нет"))
    assert delivered is True
    assert notifier.sent[-1].title == "test"


async def test_critical_alerts_bypass_dedup(monkeypatch) -> None:
    """Критические уведомления не подавляются дедупликацией."""
    notifier = AlertNotifier(min_interval_sec=3600)
    notifier.settings.alert_webhook_url = ""
    warning_first = await notifier.notify_warning("Gas spike", "gas 120 gwei")
    warning_second = await notifier.notify_warning("Gas spike", "gas 130 gwei")
    critical_first = await notifier.notify_critical("Kill switch", "engaging")
    critical_second = await notifier.notify_critical("Kill switch", "engaging again")

    assert warning_first is True
    assert warning_second is False  # подавлено дедупликацией
    assert critical_first is True
    assert critical_second is True


async def test_webhook_delivery_uses_json_payload(monkeypatch) -> None:
    """Webhook-канал отправляет JSON с уровнем и payload (контракт для n8n/PagerDuty)."""
    captured: dict = {}

    class FakeResponse:
        status_code = 202

    class FakeClient:
        async def post(self, url, json=None):  # noqa: A002 — имитируем httpx.AsyncClient
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    from pydantic import SecretStr

    notifier = AlertNotifier()
    monkeypatch.setattr(notifier.settings, "alert_webhook_url", "https://hooks.example/flashhunter")
    monkeypatch.setattr(notifier.settings, "alert_telegram_token", SecretStr(""))
    monkeypatch.setattr(notifier.settings, "alert_telegram_chat_id", "")
    monkeypatch.setattr(notifier.settings, "alert_discord_webhook", "")
    monkeypatch.setattr(notifier, "_http", lambda: _async_return(FakeClient()))

    delivered = await notifier.notify_warning("Latency", "p99 340 ms", chain_id=1)
    assert delivered is True
    assert captured["url"] == "https://hooks.example/flashhunter"
    assert captured["json"]["level"] == "warning"
    assert captured["json"]["payload"] == {"chain_id": 1}
    assert captured["json"]["source"] == "flashhunter"


async def _async_return(value):
    return value


async def test_close_is_idempotent() -> None:
    """aclose() безопасно вызывать повторно (вызывается при остановке приложения)."""
    notifier = AlertNotifier()
    await notifier.aclose()
    await notifier.aclose()
    assert notifier._client is None


def test_get_notifier_is_singleton() -> None:
    """get_notifier() отдаёт один и тот же объект (единая точка алертинга)."""
    from flashhunter.alerting import get_notifier

    assert get_notifier() is get_notifier()


@pytest.mark.parametrize("level", [AlertLevel.info, AlertLevel.warning, AlertLevel.critical])
def test_alert_levels_render(level: AlertLevel) -> None:
    """Каждому уровню соответствует своя иконка."""
    alert = Alert(title="t", message="m", level=level)
    assert alert.as_text().startswith(("ℹ️", "⚠️", "🚨"))
