import pytest
from pydantic import ValidationError

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
