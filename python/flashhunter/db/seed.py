"""Первичное наполнение БД: admin, сети, токены, стратегии, риск-лимиты, контракты.

Идемпотентно: повторный вызов обновляет только отсутствующие записи
(кроме strategy param merge — параметры стратегий дозаполняются недостающими ключами).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flashhunter.config.settings import get_settings
from flashhunter.db.models import (
    Contract,
    ContractStatus,
    Network,
    RiskLimit,
    Setting,
    StrategyConfig,
    StrategyKind,
    Token,
    User,
    Wallet,
    WalletKind,
)
from flashhunter.db.session import session_scope

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Каталог сетей (ТЗ: 15+ сетей; здесь — сети с готовыми интеграциями)
# --------------------------------------------------------------------------
NETWORKS: list[dict[str, Any]] = [
    {"name": "ethereum", "chain_id": 1, "explorer_url": "https://etherscan.io", "native_symbol": "ETH"},
    {"name": "bsc", "chain_id": 56, "explorer_url": "https://bscscan.com", "native_symbol": "BNB"},
    {"name": "arbitrum", "chain_id": 42161, "explorer_url": "https://arbiscan.io", "native_symbol": "ETH"},
    {"name": "base", "chain_id": 8453, "explorer_url": "https://basescan.org", "native_symbol": "ETH"},
    {"name": "optimism", "chain_id": 10, "explorer_url": "https://optimistic.etherscan.io", "native_symbol": "ETH"},
    {"name": "polygon", "chain_id": 137, "explorer_url": "https://polygonscan.com", "native_symbol": "POL"},
    {"name": "avalanche", "chain_id": 43114, "explorer_url": "https://snowtrace.io", "native_symbol": "AVAX"},
    {"name": "linea", "chain_id": 59144, "explorer_url": "https://lineascan.build", "native_symbol": "ETH"},
    {"name": "scroll", "chain_id": 534352, "explorer_url": "https://scrollscan.com", "native_symbol": "ETH"},
    {"name": "zksync", "chain_id": 324, "explorer_url": "https://explorer.zksync.io", "native_symbol": "ETH"},
    {"name": "blast", "chain_id": 81457, "explorer_url": "https://blastscan.io", "native_symbol": "ETH"},
    {"name": "gnosis", "chain_id": 100, "explorer_url": "https://gnosisscan.io", "native_symbol": "xDAI"},
    {"name": "celo", "chain_id": 42220, "explorer_url": "https://celoscan.io", "native_symbol": "CELO"},
    {"name": "mantle", "chain_id": 5000, "explorer_url": "https://mantlescan.xyz", "native_symbol": "MNT"},
    {"name": "sonic", "chain_id": 146, "explorer_url": "https://sonicscan.org", "native_symbol": "S"},
]

# Токены-«мажоры» по сетям: symbol -> (address, decimals)
STABLES = {
    "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
    "WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
    "WBTC": ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
}

STRATEGIES: list[dict[str, Any]] = [
    {
        "name": "dex_arb",
        "kind": StrategyKind.dex_arb,
        "enabled": True,
        "chains": [1, 8453, 42161],
        "params": {
            "min_profit_usd": 12.0,
            "max_hops": 2,
            "min_size_usd": 500.0,
            "max_size_usd": 250000.0,
            "min_roi_pct": 0.05,
            "dexes": ["uniswap_v2", "uniswap_v3", "sushiswap", "pancakeswap_v3", "curve", "balancer"],
        },
    },
    {
        "name": "triangular",
        "kind": StrategyKind.triangular,
        "enabled": True,
        "chains": [1, 8453, 42161],
        "params": {"min_profit_usd": 10.0, "max_hops": 4, "solver": "gradient_descent", "iterations": 24},
    },
    {
        "name": "stable_peg",
        "kind": StrategyKind.stable_peg,
        "enabled": True,
        "chains": [1, 137, 42161],
        "params": {"min_deviation_bps": 1.0, "max_deviation_bps": 50.0, "min_profit_usd": 8.0},
    },
    {
        "name": "liquidation",
        "kind": StrategyKind.liquidation,
        "enabled": True,
        "chains": [1, 8453, 42161, 10],
        "params": {
            "protocols": ["aave_v3", "compound_v3", "morpho_blue", "spark", "silo", "euler_v2", "fluid"],
            "max_health_factor": 1.02,
            "min_profit_usd": 40.0,
            "close_factor": 0.5,
        },
    },
    {
        "name": "jit",
        "kind": StrategyKind.jit,
        "enabled": True,
        "chains": [1, 8453],
        "params": {
            "min_swap_usd": 50000.0,
            "range_width_bps": 10,
            "jit_capital_usd": 750000.0,
            "active_depth_pct": 0.005,
            "il_bps": 0.5,
            "min_profit_usd": 5.0,
            "max_il_usd": 500.0,
        },
    },
    {
        "name": "backrun",
        "kind": StrategyKind.backrun,
        "enabled": True,
        "chains": [1, 8453],
        "params": {"min_swap_usd": 25000.0, "max_price_impact_bps": 40, "sandwich": False},
    },
    {
        "name": "intent",
        "kind": StrategyKind.intent,
        "enabled": True,
        "chains": [1, 8453, 42161],
        "params": {
            "bridges": ["across", "debridge_dln", "layerzero_oft"],
            "max_bridge_fee_bps": 25,
            "max_eta_sec": 900,
            "min_profit_usd": 25.0,
        },
    },
]

RISK_LIMITS: list[dict[str, Any]] = [
    {"scope": "global", "key": "daily_loss_limit_usd", "value": {"limit": 500.0}},
    {"scope": "global", "key": "hourly_loss_limit_usd", "value": {"limit": 150.0}},
    {"scope": "global", "key": "revert_rate_limit_pct", "value": {"limit": 20.0, "window_sec": 300}},
    {"scope": "global", "key": "latency_p99_limit_ms", "value": {"limit": 500}},
    {"scope": "global", "key": "connection_loss_sec", "value": {"limit": 10}},
    {"scope": "global", "key": "gas_ceiling_gwei", "value": {"limit": 45}},
    {"scope": "global", "key": "max_slippage_bps", "value": {"limit": 30}},
    {"scope": "chain", "key": "1", "value": {"max_exposure_usd": 150000.0}},
    {"scope": "chain", "key": "8453", "value": {"max_exposure_usd": 60000.0}},
    {"scope": "chain", "key": "42161", "value": {"max_exposure_usd": 60000.0}},
    {"scope": "token", "key": "WETH", "value": {"max_exposure_usd": 75000.0}},
    {"scope": "token", "key": "USDC", "value": {"max_exposure_usd": 75000.0}},
    {"scope": "protocol", "key": "aave_v3", "value": {"max_exposure_usd": 120000.0}},
]

SETTINGS_DEFAULTS: dict[str, Any] = {
    "kill_switch": {"active": False, "reason": "", "changed_at": ""},
    "dry_run": {"active": True, "changed_at": ""},
    "live_confirmed": {"confirmed": False, "confirmed_at": ""},
    "dry_run_started_at": {"ts": ""},
    "browser_telemetry": {"last_seen": ""},
    "relays_enabled": {
        "flashbots": True,
        "bloxroute": True,
        "eden": False,
        "merkle": True,
        "titan": True,
        "rsync": True,
        "beaver": True,
        "buildernet": True,
    },
}


async def seed_all(session: AsyncSession | None = None) -> dict[str, int]:
    """Наполняет БД базовыми сущностями. Возвращает счётчики созданных записей."""
    if session is not None:
        return await _seed(session)
    async with session_scope() as own_session:
        return await _seed(own_session)


async def _seed(session: AsyncSession) -> dict[str, int]:
    from flashhunter.api.auth import hash_password  # локальный импорт против цикла

    settings = get_settings()
    created = {"users": 0, "networks": 0, "tokens": 0, "strategies": 0, "risk_limits": 0,
               "settings": 0, "contracts": 0, "wallets": 0}

    # --- admin ------------------------------------------------------------
    result = await session.execute(select(User).where(User.username == settings.admin_default_user))
    if result.scalar_one_or_none() is None:
        session.add(
            User(
                username=settings.admin_default_user,
                password_hash=hash_password(settings.admin_default_password.get_secret_value()),
                role="admin",
                must_change_password=settings.force_password_change,
            )
        )
        created["users"] += 1

    # --- сети + токены ----------------------------------------------------
    for spec in NETWORKS:
        result = await session.execute(select(Network).where(Network.chain_id == spec["chain_id"]))
        network = result.scalar_one_or_none()
        if network is None:
            network = Network(
                name=spec["name"],
                chain_id=spec["chain_id"],
                rpc_urls=_rpc_for(spec["chain_id"], settings),
                explorer_url=spec["explorer_url"],
                native_symbol=spec["native_symbol"],
                gas_ceiling_gwei=settings.exec_max_gas_gwei,
                enabled=spec["chain_id"] in {1, 8453, 42161, 137, 10, 56},
            )
            session.add(network)
            await session.flush()
            created["networks"] += 1

        # Эти адреса — только Ethereum mainnet. Не размножаем их в записи сетей,
        # где те же адреса могут обозначать другие контракты или вообще быть пустыми.
        if spec["chain_id"] != 1:
            legacy_mainnet_addresses = [address for address, _ in STABLES.values()]
            legacy = await session.execute(
                select(Token).where(
                    Token.network_id == network.id,
                    Token.address.in_(legacy_mainnet_addresses),
                    Token.enabled.is_(True),
                )
            )
            for token in legacy.scalars():
                token.enabled = False
            continue
        for symbol, (address, decimals) in STABLES.items():
            exists = await session.execute(
                select(Token).where(Token.network_id == network.id, Token.address == address)
            )
            if exists.scalar_one_or_none() is None:
                session.add(
                    Token(
                        network_id=network.id, symbol=symbol, address=address,
                        decimals=decimals, enabled=True,
                    )
                )
                created["tokens"] += 1

    # --- стратегии --------------------------------------------------------
    for spec in STRATEGIES:
        result = await session.execute(
            select(StrategyConfig).where(StrategyConfig.name == spec["name"])
        )
        row = result.scalar_one_or_none()
        if row is None:
            session.add(
                StrategyConfig(
                    name=spec["name"], kind=spec["kind"], enabled=bool(spec["enabled"]),
                    params=spec["params"], chains=spec["chains"],
                )
            )
            created["strategies"] += 1
        else:
            merged = {**spec["params"], **(row.params or {})}
            row.params = merged

    # --- риск-лимиты ------------------------------------------------------
    for spec in RISK_LIMITS:
        result = await session.execute(
            select(RiskLimit).where(RiskLimit.scope == spec["scope"], RiskLimit.key == spec["key"])
        )
        if result.scalar_one_or_none() is None:
            session.add(RiskLimit(scope=spec["scope"], key=spec["key"], value=spec["value"]))
            created["risk_limits"] += 1

    # --- настройки --------------------------------------------------------
    for key, value in SETTINGS_DEFAULTS.items():
        if await session.get(Setting, key) is None:
            enriched = dict(value)
            if key == "dry_run_started_at":
                enriched["ts"] = datetime.now(UTC).isoformat()
            session.add(Setting(key=key, value=enriched))
            created["settings"] += 1

    # --- контракты и кошельки ---------------------------------------------
    eth = (await session.execute(select(Network).where(Network.chain_id == 1))).scalar_one_or_none()
    if eth is not None:
        for name, address in (
            ("FlashLoanArbitrage", settings.flashloan_arbitrage_address),
            ("Liquidator", settings.liquidator_address),
            ("JITVault", settings.jit_vault_address),
        ):
            exists = await session.execute(
                select(Contract).where(Contract.network_id == eth.id, Contract.name == name)
            )
            if exists.scalar_one_or_none() is None:
                session.add(
                    Contract(
                        network_id=eth.id, name=name, address=address, abi_json=None,
                        status=ContractStatus.deployed if address else ContractStatus.undeployed,
                        verified=bool(address),
                        deployed_at=datetime.now(UTC).replace(tzinfo=None) if address else None,
                    )
                )
                created["contracts"] += 1

    hot = await session.execute(select(Wallet).where(Wallet.kind == WalletKind.hot))
    if hot.scalars().first() is None:
        session.add(
            Wallet(
                name="hot-executor-01",
                address="0x000000000000000000000000000000000000dEaD",
                encrypted_key=None,
                hsm_ref="local-keystore://hot-executor-01",
                is_hot=True,
                kind=WalletKind.hot,
                chain_scope=[1, 8453, 42161],
                balance_usd=Decimal("0"),
            )
        )
        created["wallets"] += 1

    await session.flush()
    log.info("seed completed: %s", created)
    return created


def _rpc_for(chain_id: int, settings: Any) -> dict[str, str]:
    """Собирает RPC-URL-ы сети из настроек окружения."""
    mapping = {
        1: (settings.eth_rpc_http, settings.eth_rpc_ws),
        56: (settings.bsc_rpc_http, ""),
        42161: (settings.arb_rpc_http, ""),
        8453: (settings.base_rpc_http, ""),
        10: (settings.op_rpc_http, ""),
        137: (settings.polygon_rpc_http, ""),
        43114: (settings.avalanche_rpc_http, ""),
    }
    http, ws = mapping.get(chain_id, ("", ""))
    return {"http": http or f"https://rpc-placeholder.invalid/{chain_id}", "ws": ws}
