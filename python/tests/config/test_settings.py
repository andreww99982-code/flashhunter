import os

import pytest
from pydantic import ValidationError

from flashhunter.config.panel_config import save_panel_settings
from flashhunter.config.secrets import decrypt_secret, encrypt_secret
from flashhunter.config.settings import Settings


def test_prod_requires_real_market_data() -> None:
    with pytest.raises(ValidationError, match="MOCK_MARKET_DATA must be false in prod"):
        Settings(
            FLASHHUNTER_ENV="prod",
            JWT_SECRET="a" * 64,
            ADMIN_DEFAULT_PASSWORD="a-strong-password",
            MOCK_MARKET_DATA=True,
            ETH_RPC_HTTP="https://rpc.example",
        )


def test_prod_requires_rpc_endpoint() -> None:
    with pytest.raises(ValidationError, match="A live RPC endpoint is required"):
        Settings(
            FLASHHUNTER_ENV="prod",
            JWT_SECRET="a" * 64,
            ADMIN_DEFAULT_PASSWORD="a-strong-password",
            MOCK_MARKET_DATA=False,
            ETH_RPC_HTTP="",
            ETH_RPC_FALLBACKS="",
        )


def test_panel_settings_override_environment_before_startup(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("FLASHHUNTER_SETTINGS_FILE", str(settings_path))
    monkeypatch.setenv("API_PORT", "8001")
    save_panel_settings({"api_port": 8123})

    assert Settings().api_port == 8123


def test_encrypted_settings_survive_jwt_rotation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FLASHHUNTER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("JWT_SECRET", "old-jwt-secret")
    encrypted = encrypt_secret("sensitive-rpc-credential")
    monkeypatch.setenv("JWT_SECRET", "new-jwt-secret")

    assert decrypt_secret(encrypted) == "sensitive-rpc-credential"
    if os.name == "posix":
        assert (tmp_path / "settings.key").stat().st_mode & 0o777 == 0o600


def test_panel_configuration_is_loaded_before_database_settings(tmp_path, monkeypatch) -> None:
    from flashhunter.config.panel_config import save_panel_settings

    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("FLASHHUNTER_SETTINGS_FILE", str(settings_path))
    rpc_secret = "https://private-rpc.example/credential"
    encrypted_rpc = encrypt_secret(rpc_secret)
    save_panel_settings({"db_mode": "postgres", "postgres_dsn": "postgresql+asyncpg://db/live"})
    save_panel_settings({"eth_rpc_http": {"encrypted": encrypted_rpc}})

    configured = Settings()
    assert configured.db_mode == "postgres"
    assert configured.postgres_dsn == "postgresql+asyncpg://db/live"
    assert configured.eth_rpc_http == rpc_secret
    assert rpc_secret not in settings_path.read_text(encoding="utf-8")
