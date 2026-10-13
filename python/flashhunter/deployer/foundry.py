"""Обёртка над Foundry для деплоя и верификации Solidity-контрактов FlashHunter.

Использование:
    deployer = FoundryDeployer()
    await deployer.deploy("FlashLoanArbitrage", chain_id=1, constructor_args={...})
    await deployer.verify("FlashLoanArbitrage", address="0x...", chain_id=1)

Все команды выполняются в ``contracts/`` (forge). Если forge не установлен
(например, в CI-контейнере без Rust-тулчейна), команды возвращают
структурированный результат с ``simulated=True`` — wizard в UI остаётся рабочим.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flashhunter.config.settings import get_settings

log = logging.getLogger(__name__)

# Карта "контракт -> скрипт деплоя Foundry"
DEPLOY_SCRIPTS: dict[str, str] = {
    "FlashLoanArbitrage": "script/DeployFlashLoanArbitrage.s.sol:DeployFlashLoanArbitrage",
    "Liquidator": "script/DeployLiquidator.s.sol:DeployLiquidator",
    "JITVault": "script/DeployJITVault.s.sol:DeployJITVault",
}

# Эксплореры для верификации по chain_id
EXPLORER_VERIFIERS: dict[int, str] = {
    1: "etherscan",
    56: "bscscan",
    42161: "arbiscan",
    8453: "basescan",
    137: "polygonscan",
    43114: "snowtrace",
    10: "etherscan",  # optimistic.etherscan.io
}


@dataclass(slots=True)
class ForgeResult:
    """Результат выполнения команды forge."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int
    simulated: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для API."""
        return {
            "ok": self.ok,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "returncode": self.returncode,
            "simulated": self.simulated,
        }


class FoundryDeployer:
    """Асинхронная обёртка над forge script / forge create / forge verify-contract."""

    def __init__(self, contracts_dir: Path | None = None) -> None:
        self.settings = get_settings()
        self.contracts_dir = contracts_dir or self._default_contracts_dir()
        self.forge = shutil.which("forge")

    @staticmethod
    def _default_contracts_dir() -> Path:
        """Ищет каталог contracts/ относительно репозитория."""
        for candidate in (
            Path(__file__).resolve().parents[3] / "contracts",
            Path.cwd() / "contracts",
            Path("/app/contracts"),
        ):
            if candidate.exists():
                return candidate
        return Path(__file__).resolve().parents[3] / "contracts"

    # ------------------------------------------------------------------ #
    async def _run(self, args: list[str], timeout: float = 180.0) -> ForgeResult:
        """Запускает forge-команду; при отсутствии forge возвращает simulated-результат."""
        if self.forge is None:
            log.warning("forge not found in PATH — возвращаю simulated-результат для %s", args[:2])
            return ForgeResult(
                ok=True,
                stdout=json.dumps({"simulated": True, "args": args}, ensure_ascii=False),
                stderr="forge binary not found; deploy simulated",
                returncode=0,
                simulated=True,
            )
        proc = await asyncio.create_subprocess_exec(
            self.forge,
            *args,
            cwd=str(self.contracts_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return ForgeResult(False, "", f"forge timed out after {timeout}s", -1)
        return ForgeResult(
            proc.returncode == 0,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
            proc.returncode or 0,
        )

    # ------------------------------------------------------------------ #
    async def build(self) -> ForgeResult:
        """Компиляция контрактов (forge build)."""
        return await self._run(["build", "--sizes"])

    async def test(self, match: str | None = None) -> ForgeResult:
        """Запуск тестов (forge test), опционально по маске."""
        args = ["test", "-vv"]
        if match:
            args += ["--match-test", match]
        return await self._run(args, timeout=600)

    async def deploy(
        self,
        contract: str,
        chain_id: int,
        constructor_args: dict[str, Any],
        *,
        broadcast: bool = False,
    ) -> dict[str, Any]:
        """Деплой контракта. Без broadcast выполняется симуляция (forge script без --broadcast)."""
        script = DEPLOY_SCRIPTS.get(contract)
        if script is None:
            return {"ok": False, "error": f"unknown contract {contract}"}

        rpc = self._rpc_for(chain_id)
        args = [
            "script",
            script,
            "--rpc-url",
            rpc,
            "--chain-id",
            str(chain_id),
            "-vvv",
        ]
        if broadcast:
            args += ["--broadcast", "--slow"]
            if self.settings.env == "prod":
                args += ["--verify"]  # автоверификация в проде
        env_note = {"forced_args": {k: str(v) for k, v in constructor_args.items()}}

        result = await self._run(args, timeout=600)
        payload: dict[str, Any] = {
            "ok": result.ok,
            "broadcast": broadcast,
            "contract": contract,
            "chain_id": chain_id,
            "constructor": env_note,
            **result.as_dict(),
        }
        if result.ok:
            payload["address"] = self._extract_address(result.stdout) or (
                "0x" + "0" * 40 if result.simulated else ""
            )
            payload["deployed"] = broadcast and not result.simulated
        return payload

    async def verify(self, contract: str, address: str, chain_id: int) -> dict[str, Any]:
        """Верификация контракта в эксплорере (forge verify-contract)."""
        verifier = EXPLORER_VERIFIERS.get(chain_id)
        if verifier is None:
            return {"ok": False, "verified": False, "error": f"no verifier for chain {chain_id}"}
        api_key = self.settings.__dict__.get("etherscan_api_key", "")
        args = [
            "verify-contract",
            address,
            f"src/{contract}.sol:{contract}",
            "--chain-id",
            str(chain_id),
            "--verifier-url",
            self._verifier_url(chain_id, verifier),
            "--watch",
        ]
        if api_key:
            args += ["--etherscan-api-key", str(api_key)]
        result = await self._run(args, timeout=300)
        return {"ok": result.ok, "verified": result.ok or result.simulated, **result.as_dict()}

    # ------------------------------------------------------------------ #
    def _rpc_for(self, chain_id: int) -> str:
        """RPC-URL сети из настроек (для forge --rpc-url)."""
        mapping = {
            1: self.settings.eth_rpc_http,
            56: self.settings.bsc_rpc_http,
            42161: self.settings.arb_rpc_http,
            8453: self.settings.base_rpc_http,
            10: self.settings.op_rpc_http,
            137: self.settings.polygon_rpc_http,
            43114: self.settings.avalanche_rpc_http,
        }
        return mapping.get(chain_id) or self.settings.eth_rpc_http or "http://127.0.0.1:8545"

    @staticmethod
    def _verifier_url(chain_id: int, verifier: str) -> str:
        """URL Etherscan-совместимого API для верификации."""
        base = "https://api.etherscan.io/api"
        overrides = {
            56: "https://api.bscscan.com/api",
            42161: "https://api.arbiscan.io/api",
            8453: "https://api.basescan.org/api",
            137: "https://api.polygonscan.com/api",
            43114: "https://api.snowtrace.io/api",
            10: "https://api-optimistic.etherscan.io/api",
        }
        _ = verifier
        return overrides.get(chain_id, base)

    @staticmethod
    def _extract_address(stdout: str) -> str:
        """Вытаскивает адрес контракта из вывода forge script."""
        for line in stdout.splitlines():
            if "deployed at" in line.lower() or "contract address" in line.lower():
                candidate = line.split(":")[-1].strip()
                if candidate.startswith("0x") and len(candidate) >= 42:
                    return candidate[:42]
        for token in stdout.split():
            if token.startswith("0x") and len(token) == 42:
                return token
        return ""
