"""Интеграционные тесты HTTP-API (все 12 модулей маршрутов + метрики + SPA)."""

from __future__ import annotations

import pytest
from eth_account import Account

API = "/api/v1"


# --------------------------------------------------------------------------- #
# health / config / SPA
# --------------------------------------------------------------------------- #
def test_health_and_public_config(api_client) -> None:
    """/health отдаёт состояние сервиса, /config/public — безопасный срез настроек."""
    health = api_client.get(f"{API}/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] in {"ok", "degraded"}
    assert "version" in body

    public = api_client.get(f"{API}/config/public")
    assert public.status_code == 200
    config = public.json()
    assert set(config) >= {"dry_run", "mock_market_data", "force_password_change", "ui_available"}
    # секреты наружу не уходят
    assert "secret_key" not in config
    assert "jwt_secret" not in config


def test_runtime_config_api_lists_fields_without_revealing_secrets(api_client) -> None:
    response = api_client.get(f"{API}/settings/config")
    assert response.status_code == 200
    fields = {field["name"]: field for field in response.json()["fields"]}
    assert fields["eth_rpc_http"]["kind"] == "secret"
    assert fields["eth_rpc_http"]["value"] is None
    assert fields["eth_rpc_fallbacks"]["kind"] == "secret"
    assert "keeper_private_key" in fields
    assert "risk_daily_loss_limit_usd" in fields
    locked = {field["name"]: field for field in response.json()["locked_fields"]}
    assert locked["mock_market_data"]["reason"].startswith("Отключено")


def test_runtime_config_api_validates_values(api_client) -> None:
    response = api_client.put(f"{API}/settings/config", json={"values": {"api_port": "not-a-port"}})
    assert response.status_code == 422

    unsupported = api_client.put(
        f"{API}/settings/config", json={"values": {"jwt_secret": "must-not-be-editable-here"}}
    )
    assert unsupported.status_code == 422

    mock_market = api_client.put(f"{API}/settings/config", json={"values": {"mock_market_data": True}})
    assert mock_market.status_code == 422


def test_metrics_endpoint_is_prometheus_text(api_client) -> None:
    """/metrics — Prometheus text format с ключевыми метриками."""
    response = api_client.get("/metrics")
    assert response.status_code == 200
    text = response.text
    assert "flashhunter_service_up" in text
    assert "flashhunter_opportunities_total" in text
    assert "flashhunter_dry_run_active" in text


def test_spa_served_from_assets(api_client) -> None:
    """Корень и глубокие ссылки отдают собранный SPA (React Router)."""
    root = api_client.get("/")
    assert root.status_code == 200
    assert 'id="root"' in root.text

    for path in ("/dashboard", "/settings", "/latency"):
        assert 'id="root"' in api_client.get(path).text

    # статика доступна
    assert api_client.get("/api/docs").status_code == 200


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_login_wrong_password(api_client) -> None:
    """Неверный пароль — 401 без подсказок о существовании пользователя."""
    response = api_client.post(f"{API}/auth/login", json={"username": "admin", "password": "totally-wrong"})
    assert response.status_code == 401


def test_me_returns_profile(api_client) -> None:
    """Профиль текущего пользователя содержит роль и флаги безопасности."""
    response = api_client.get(f"{API}/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "admin"
    assert body["role"] in {"admin", "operator", "viewer"}
    assert "totp_enabled" in body


async def test_change_password_policy_and_rotation(api_client) -> None:
    """Смена пароля: проверка текущего пароля, политика, ротация токена и вход с новым паролем."""
    # 1. Неверный текущий пароль
    wrong = api_client.post(
        f"{API}/auth/change-password",
        json={"current_password": "not-my-password", "new_password": "FlashHunter-Strong-2026!"},
    )
    assert wrong.status_code == 401

    # 2. Слабый новый пароль отклоняется политикой
    weak = api_client.post(
        f"{API}/auth/change-password",
        json={"current_password": "admin", "new_password": "qwerty123"},
    )
    assert weak.status_code == 422

    # 3. Успешная смена: старый токен перестаёт работать, новый — работает
    new_password = "FlashHunter-Strong-2026!"
    try:
        changed = api_client.post(
            f"{API}/auth/change-password",
            json={"current_password": "admin", "new_password": new_password},
        )
        assert changed.status_code == 200, changed.text
        fresh_token = changed.json()["token"]
        assert fresh_token

        api_client.headers.update({"Authorization": f"Bearer {fresh_token}"})
        assert api_client.get(f"{API}/auth/me").status_code == 200
        assert api_client.get(f"{API}/auth/me").json()["must_change_password"] is False

        relogin = api_client.post(f"{API}/auth/login", json={"username": "admin", "password": new_password})
        assert relogin.status_code == 200
        assert relogin.json()["must_change_password"] is False
    finally:
        # возвращаем дефолтный пароль прямо в БД: остальные тесты логинятся как admin/admin
        await _reset_admin_password()


async def _reset_admin_password() -> None:
    """Тестовый хелпер: восстанавливает пароль админа (в prod так делать нельзя)."""
    from sqlalchemy import select

    from flashhunter.api.auth import hash_password
    from flashhunter.db.models import User
    from flashhunter.db.session import session_scope

    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.username == "admin"))).scalar_one()
        user.password_hash = hash_password("admin")
        user.must_change_password = False


def test_totp_setup_returns_qr_and_secret(api_client) -> None:
    """Первичная настройка 2FA отдаёт секрет и QR (data-URI, без внешних картинок)."""
    response = api_client.post(f"{API}/auth/totp/setup")
    assert response.status_code == 200
    body = response.json()
    assert len(body["secret"]) >= 16
    assert body["qr"].startswith("data:image")


def test_protected_endpoint_requires_token() -> None:
    """Без токена защищённые ручки недоступны."""
    from fastapi.testclient import TestClient

    from flashhunter.api.app import app

    with TestClient(app) as client:
        assert client.get(f"{API}/dashboard/overview").status_code in {401, 403}


# --------------------------------------------------------------------------- #
# dashboard / opportunities / strategies
# --------------------------------------------------------------------------- #
def test_dashboard_overview_structure(api_client) -> None:
    """Дашборд отдаёт PnL, кривую капитала, кошельки, сервисы и риск-состояние."""
    response = api_client.get(f"{API}/dashboard/overview")
    assert response.status_code == 200
    body = response.json()
    for key in ("pnl", "equity_curve", "wallets", "services", "risk", "dry_run", "kill_switch"):
        assert key in body
    assert set(body["pnl"]) >= {"24h", "7d", "30d", "all"}
    assert len(body["services"]["relays"]) >= 5


def test_opportunities_list_and_stats(api_client) -> None:
    """Список возможностей поддерживает фильтры и пагинацию, /stats — агрегаты."""
    response = api_client.get(f"{API}/opportunities?limit=10&live_only=false")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total"} <= set(body)
    assert body["total"] >= 0

    filtered = api_client.get(f"{API}/opportunities?live_only=false&strategy=dex_arb&min_profit=1")
    assert filtered.status_code == 200

    stats = api_client.get(f"{API}/opportunities/stats")
    assert stats.status_code == 200
    assert "strategies" in stats.json()


def test_manual_execution_with_unknown_uid(api_client) -> None:
    """Ручное исполнение с несуществующим uid возвращает понятную ошибку."""
    response = api_client.post(f"{API}/opportunities/execute", json={"uid": "does-not-exist"})
    assert response.status_code in {404, 409, 410}


def test_strategies_list_patch_and_toggle(api_client) -> None:
    """Стратегии: чтение, включение/выключение, изменение параметров."""
    response = api_client.get(f"{API}/strategies")
    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["name"] for item in items} == {
        "dex_arb",
        "triangular",
        "stable_peg",
        "liquidation",
        "jit",
        "backrun",
        "intent",
    }

    patch = api_client.patch(f"{API}/strategies/jit", json={"enabled": False, "params": {"min_swap_usd": 75000}})
    assert patch.status_code == 200
    assert patch.json()["enabled"] is False

    toggle = api_client.post(f"{API}/strategies/jit/toggle")
    assert toggle.status_code == 200
    assert toggle.json()["enabled"] is True

    unknown = api_client.patch(f"{API}/strategies/nope", json={"enabled": True})
    assert unknown.status_code == 404


# --------------------------------------------------------------------------- #
# settings: лимиты, kill-switch, режим, api-ключи
# --------------------------------------------------------------------------- #
def test_settings_snapshot_and_limit_update(api_client) -> None:
    """Изменение риск-лимита применяется к живому риск-движку немедленно."""
    before = api_client.get(f"{API}/settings").json()
    assert {"risk", "limits", "settings", "api_keys", "strategies_available"} <= set(before)

    response = api_client.put(
        f"{API}/settings/limits",
        json={"scope": "global", "key": "gas_ceiling_gwei", "value": {"limit": 42}},
    )
    assert response.status_code == 200

    from flashhunter.risk.engine import get_risk_engine

    assert float(get_risk_engine().gas_ceiling_gwei) == 42.0


def test_kill_switch_engage_and_release(api_client) -> None:
    """Kill-switch из UI останавливает и возобновляет исполнение."""
    engage = api_client.post(f"{API}/settings/kill-switch", json={"engage": True, "reason": "api test"})
    assert engage.status_code == 200
    from flashhunter.risk.engine import get_risk_engine

    assert get_risk_engine().kill_switch is True

    release = api_client.post(f"{API}/settings/kill-switch", json={"engage": False, "reason": "api test release"})
    assert release.status_code == 200
    assert get_risk_engine().kill_switch is False


def test_live_mode_requires_confirmation_word(api_client) -> None:
    """Переход в live без слова LIVE отклоняется (422)."""
    response = api_client.post(f"{API}/settings/mode", json={"live": True, "confirmation": "yes, please"})
    assert response.status_code == 422


def test_live_mode_requires_dry_run_period(api_client) -> None:
    """Переход в live до истечения 24 ч dry-run отклоняется (409)."""
    response = api_client.post(f"{API}/settings/mode", json={"live": True, "confirmation": "LIVE"})
    assert response.status_code == 409
    assert "24" in response.json()["detail"]


def test_back_to_dry_run_is_allowed(api_client) -> None:
    """Возврат в dry-run доступен всегда."""
    response = api_client.post(f"{API}/settings/mode", json={"live": False})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True


def test_api_keys_crud(api_client) -> None:
    """Ключи внешних сервисов добавляются и удаляются, значение маскируется."""
    created = api_client.post(f"{API}/settings/api-keys", json={"provider": "etherscan", "api_key": "ABC123SECRET"})
    assert created.status_code == 200
    key_id = created.json()["id"]

    listed = api_client.get(f"{API}/settings").json()["api_keys"]
    row = next(item for item in listed if item["id"] == key_id)
    assert "ABC123SECRET" not in row["masked"]
    assert row["masked"].endswith("CRET") or row["masked"].startswith("••••")

    deleted = api_client.delete(f"{API}/settings/api-keys/{key_id}")
    assert deleted.status_code == 200


def test_settings_ping_measures_roundtrip(api_client) -> None:
    """Ping отдаёт server-time для оценки задержки панели."""
    response = api_client.get(f"{API}/settings/ping")
    assert response.status_code == 200
    assert "ts" in response.json()


# --------------------------------------------------------------------------- #
# history / analytics / latency / logs
# --------------------------------------------------------------------------- #
def test_history_list_detail_and_csv(api_client) -> None:
    """История: список с пагинацией, деталь сделки и CSV-экспорт с BOM."""
    listed = api_client.get(f"{API}/history?page=1&page_size=10")
    assert listed.status_code == 200
    assert "items" in listed.json()

    csv = api_client.get(f"{API}/history/export/csv?limit=100")
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.content.startswith(b"\xef\xbb\xbf")  # BOM для Excel

    missing = api_client.get(f"{API}/history/999999")
    assert missing.status_code == 404


def test_analytics_summary_and_rollup(api_client) -> None:
    """Аналитика отдаёт PnL, разбивки и кривую; rollup пересчитывает дневные агрегаты."""
    for period in ("24h", "7d", "30d", "all"):
        response = api_client.get(f"{API}/analytics/summary?period={period}")
        assert response.status_code == 200
        body = response.json()
        assert {"pnl", "equity_curve", "by_strategy", "by_chain", "daily", "relay_stats"} <= set(body)

    rollup = api_client.post(f"{API}/analytics/rollup")
    assert rollup.status_code == 200
    assert "rows" in rollup.json()


def test_latency_metrics_and_histogram(api_client) -> None:
    """Латентность: бюджеты сервисов из ТЗ §1.2 и гистограмма задержек."""
    response = api_client.get(f"{API}/latency/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "budgets_us" in body and "services" in body and "live" in body
    budgets = body["budgets_us"]
    assert budgets["ingestor"] == 5000
    assert budgets["sim-engine"] == 8000
    assert budgets["relay-client"] == 10000

    histogram = api_client.get(f"{API}/latency/histogram?buckets=8")
    assert histogram.status_code == 200
    assert "buckets" in histogram.json()


def test_logs_events_and_audit(api_client) -> None:
    """Логи: поток событий оркестратора и журнал аудита действий оператора."""
    events = api_client.get(f"{API}/logs/events?limit=50")
    assert events.status_code == 200
    assert "items" in events.json()

    audit = api_client.get(f"{API}/logs/audit?page=1&page_size=50")
    assert audit.status_code == 200
    actions = {row["action"] for row in audit.json()["items"]}
    assert actions  # предыдущие тесты (лимиты, kill-switch) оставили след


# --------------------------------------------------------------------------- #
# contracts / wallets
# --------------------------------------------------------------------------- #
def test_contracts_registry_and_wizard(api_client) -> None:
    """Реестр контрактов и шаги wizard-а деплоя."""
    response = api_client.get(f"{API}/contracts")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "wizard"} <= set(body)
    assert set(body["wizard"]["contracts"]) == {"FlashLoanArbitrage", "Liquidator", "JITVault"}
    assert len(body["wizard"]["steps"]) >= 3


def test_contract_deploy_simulated_without_foundry(api_client) -> None:
    """Без установленного forge деплой проходит в режиме симуляции (не падает)."""
    response = api_client.post(
        f"{API}/contracts/deploy",
        json={
            "contract": "FlashLoanArbitrage",
            "chain_id": 1,
            "constructor_args": {"initialPool": "0x" + "11" * 20},
            "broadcast": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_wallets_crud_key_and_sweep(api_client) -> None:
    """Кошелёк: создание, установка ключа (тело запроса), refill-план и сивип."""
    private_key = "0x" + "cd" * 32
    address = Account.from_key(private_key).address
    created = api_client.post(
        f"{API}/wallets",
        json={
            "name": "unit-hot",
            "address": address,
            "private_key": private_key,
            "is_hot": True,
            "chain_scope": [1, 8453],
        },
    )
    assert created.status_code == 200
    wallet_id = created.json()["id"]

    listed = api_client.get(f"{API}/wallets")
    assert listed.status_code == 200
    body = listed.json()
    assert body["sweep"]["interval_sec"] >= 0
    assert any(item["id"] == wallet_id for item in body["items"])
    assert all("encrypted_key" not in item for item in body["items"])  # ключ наружу не уходит

    invalid_key = api_client.post(f"{API}/wallets/{wallet_id}/key", json={"private_key": "0x" + "ab" * 32})
    assert invalid_key.status_code == 422

    key_body = api_client.post(f"{API}/wallets/{wallet_id}/key", json={"private_key": private_key})
    assert key_body.status_code == 200
    assert key_body.json()["masked"].endswith(private_key[-4:])

    selected = api_client.post(f"{API}/settings/trading-wallet/{wallet_id}")
    assert selected.status_code == 200
    assert selected.json()["restart_required"] is True
    assert any(
        item["id"] == wallet_id and item["is_trading"]
        for item in api_client.get(f"{API}/wallets").json()["items"]
    )

    # ключ не должен приниматься как query-параметр (иначе утечёт в логи)
    assert api_client.post(f"{API}/wallets/{wallet_id}/key?private_key=0x{'ff' * 32}").status_code == 422

    refill = api_client.post(f"{API}/wallets/{wallet_id}/refill", json={"chain_id": 1, "amount_native": 0.25})
    assert refill.status_code == 200
    assert refill.json()["plan"]["dry_run"] is True

    plan = api_client.get(f"{API}/wallets/sweep/plan")
    assert plan.status_code == 200
    assert {"accumulated_usd", "threshold_usd", "will_sweep", "cold_wallet"} <= set(plan.json())

    sweep = api_client.post(f"{API}/wallets/sweep/run")
    assert sweep.status_code == 200
    assert sweep.json()["ok"] is True


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def test_backtest_profiles_and_run(api_client) -> None:
    """Бэктест: профили рынка, прогон стратегий и кривая капитала."""
    profiles = api_client.get(f"{API}/backtest/profiles")
    assert profiles.status_code == 200
    assert set(profiles.json()["profiles"]) == {"calm", "normal", "volatile", "stress"}

    response = api_client.post(
        f"{API}/backtest/run",
        json={"blocks": 40, "seed": 5, "profile": "volatile", "strategies": ["dex_arb", "triangular"], "min_profit_usd": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blocks"] == 40
    assert body["total_found"] > 0
    assert len(body["equity_curve"]) == 40
    assert {row["strategy"] for row in body["by_strategy"]} == {"dex_arb", "triangular"}


def test_backtest_rejects_unknown_strategy(api_client) -> None:
    """Неизвестная стратегия в бэктесте отклоняется схемой запроса."""
    response = api_client.post(f"{API}/backtest/run", json={"blocks": 5, "strategies": ["sandwich_attack"]})
    assert response.status_code in {422, 400}


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
def test_websocket_requires_token() -> None:
    """WS без токена не отдаёт данные (соединение закрывается кодом 4401)."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from flashhunter.api.app import app

    with TestClient(app) as anonymous:
        try:
            with anonymous.websocket_connect("/ws") as websocket:
                websocket.receive_text()
        except WebSocketDisconnect as disconnect:
            assert disconnect.code == 4401
        else:
            pytest.fail("WS без токена не был закрыт")


def test_websocket_streams_state(api_client) -> None:
    """С токеном WS отдаёт снапшот состояния оркестратора."""
    import json

    token = api_client.headers["Authorization"].split()[1]
    with api_client.websocket_connect(f"/ws?token={token}") as websocket:
        for _ in range(5):
            message = json.loads(websocket.receive_text())
            if message["type"] == "state":
                assert "dry_run" in message["data"]
                return
    pytest.fail("WS не прислал снапшот состояния")
