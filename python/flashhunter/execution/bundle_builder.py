"""Сборка bundle: [funding_tx?, arb_tx, tip_tx?] + сводка для релеев.

ВАЖНО о разделении ответственности: в production calldata и подпись формирует
Rust-сервис ``bundle-builder`` (alloy-signer + nonce-manager + HSM/KMS), а этот
модуль — управляющий контур: он готовит план вызова, оценивает газ, считает
идентификатор bundle и ведёт учёт замен (replacement) по nonce.

Просмотр calldata в UI — предпросмотр для оператора, а не боевой payload.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from flashhunter.config.settings import get_settings
from flashhunter.market.snapshot import MarketSnapshot
from flashhunter.strategies.base import Opportunity

# Фрагменты ABI контракта FlashLoanArbitrage (для предпросмотра вызова)
ARB_FUNCTION = "executeArbitrage(address,uint256,address,uint256,bytes)"
LIQ_FUNCTION = "liquidate(address,address,address,uint256,uint256,bool)"
JIT_FUNCTION = "jitMint(address,int24,int24,uint128)"

# Известные селекторы (используются для распознавания calldata в мемпуле)
KNOWN_SELECTORS = {
    "0x3593564c": "UniversalRouter.execute",
    "0x04e45aaf": "UniswapV3.exactInputSingle",
    "0x38ed1739": "UniswapV2.swapExactTokensForTokens",
    "0x7ff36ab5": "UniswapV2.swapExactETHForTokens",
    "0x5ae401dc": "Multicall.aggregate",
    "0x1f00ca74": "Curve.exchange",
    "0x5c38449e": "AaveV3.flashLoan",
    "0x42b0b77c": "AaveV3.flashLoanSimple",
    "0x2e1a7d4d": "WETH.withdraw",
}


@dataclass(slots=True)
class BundlePlan:
    """План отправки bundle (итог сборки)."""

    chain_id: int
    target: str
    calldata_preview: str
    value: int
    gas_limit: int
    max_fee_per_gas_wei: int
    max_priority_fee_per_gas_wei: int
    nonce: int
    bundle_hash: str
    txs: list[dict[str, Any]] = field(default_factory=list)
    sender: str = ""
    created_at: float = field(default_factory=time.time)
    strategy: str = ""
    opportunity_uid: str = ""
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Сериализация для API/WS/БД."""
        return {
            "chain_id": self.chain_id,
            "target": self.target,
            "calldata_preview": self.calldata_preview,
            "value": self.value,
            "gas_limit": self.gas_limit,
            "max_fee_per_gas_wei": self.max_fee_per_gas_wei,
            "max_priority_fee_per_gas_wei": self.max_priority_fee_per_gas_wei,
            "nonce": self.nonce,
            "bundle_hash": self.bundle_hash,
            "sender": self.sender,
            "strategy": self.strategy,
            "opportunity_uid": self.opportunity_uid,
            "dry_run": self.dry_run,
        }


class NonceManager:
    """Учёт nonce по (chain_id, address): выдаёт монотонно растущие значения."""

    def __init__(self) -> None:
        self._nonces: dict[tuple[int, str], int] = {}

    def seed(self, chain_id: int, address: str, nonce: int) -> None:
        """Задаёт стартовое значение nonce из RPC (при старте / после ресета)."""
        self._nonces[(chain_id, address.lower())] = nonce

    def next(self, chain_id: int, address: str) -> int:
        """Выдаёт следующий nonce."""
        key = (chain_id, address.lower())
        current = self._nonces.get(key, 0)
        self._nonces[key] = current + 1
        return current

    def peek(self, chain_id: int, address: str) -> int:
        """Текущее (неиспользованное) значение nonce."""
        return self._nonces.get((chain_id, address.lower()), 0)


class BundleBuilder:
    """Готовит bundle-план по найденной возможности."""

    def __init__(self, nonces: NonceManager | None = None) -> None:
        self.settings = get_settings()
        self.nonces = nonces or NonceManager()

    # ------------------------------------------------------------------ #
    def build(
        self,
        opp: Opportunity,
        snapshot: MarketSnapshot,
        *,
        gas_used: int,
        sender: str = "",
    ) -> BundlePlan:
        """Собирает план: цель, газ, nonce, хэш bundle, предпросмотр calldata."""
        target = {
            "cycle": self.settings.flashloan_arbitrage_address or "0xFlashLoanArbitrage",
            "liquidation": self.settings.liquidator_address or "0xLiquidator",
            "jit": self.settings.jit_vault_address or "0xJITVault",
            "backrun": self.settings.flashloan_arbitrage_address or "0xFlashLoanArbitrage",
            "intent": "0xBridgeAdapter",
        }.get(opp.kind, self.settings.flashloan_arbitrage_address or "0xFlashLoanArbitrage")

        base_fee_wei = int(snapshot.base_fee_gwei * 1e9)
        priority_wei = int(float(self.settings.exec_priority_fee_gwei) * 1e9)
        max_fee_wei = int(base_fee_wei * 1.25) + priority_wei  # запас на рост base fee
        nonce = self.nonces.next(opp.chain_id, sender or "hot-executor")

        calldata = self._encode_preview(opp)
        bundle_hash = self._hash_bundle(opp, calldata, nonce)

        txs = [
            {
                "to": target,
                "data": calldata,
                "gas": gas_used,
                "value": 0,
                "maxFeePerGas": max_fee_wei,
                "maxPriorityFeePerGas": priority_wei,
                "nonce": nonce,
                "chainId": opp.chain_id,
            }
        ]
        # tip_tx: чаевые билдеру отдельным переводом (используется частью релеев)
        tip_usd = Decimal(str(opp.meta.get("tip_usd", "0")))
        if tip_usd > 0:
            txs.append(
                {
                    "to": opp.meta.get("builder_address", "0xBuilderTip"),
                    "data": "0x",
                    "gas": 21_000,
                    "value": int(tip_usd * (Decimal(10) ** 18) / snapshot.native_price_usd),
                    "maxFeePerGas": max_fee_wei,
                    "maxPriorityFeePerGas": priority_wei,
                    "nonce": self.nonces.next(opp.chain_id, sender or "hot-executor"),
                    "chainId": opp.chain_id,
                }
            )

        return BundlePlan(
            chain_id=opp.chain_id,
            target=target,
            calldata_preview=calldata,
            value=0,
            gas_limit=gas_used,
            max_fee_per_gas_wei=max_fee_wei,
            max_priority_fee_per_gas_wei=priority_wei,
            nonce=nonce,
            bundle_hash=bundle_hash,
            txs=txs,
            sender=sender,
            strategy=opp.strategy,
            opportunity_uid=opp.uid,
            dry_run=self.settings.dry_run,
        )

    # ------------------------------------------------------------------ #
    def build_replacement(self, plan: BundlePlan, bump_pct: int | None = None) -> BundlePlan:
        """Замена «зависшего» bundle: тот же nonce, газ +bump% (по умолчанию +15%).

        Возвращает НОВЫЙ план (исходный не мутируется) — важно для журнала: в БД
        должны остаться обе записи (исходная и замена) для разбора инцидентов.
        """
        bump = bump_pct or self.settings.exec_replacement_bump_pct
        factor = (100 + bump) / 100
        replacement = replace(plan)
        replacement.txs = [dict(tx) for tx in plan.txs]
        replacement.max_fee_per_gas_wei = int(plan.max_fee_per_gas_wei * factor)
        replacement.max_priority_fee_per_gas_wei = int(plan.max_priority_fee_per_gas_wei * factor)
        for tx in replacement.txs:
            tx["maxFeePerGas"] = int(tx["maxFeePerGas"] * factor)
            tx["maxPriorityFeePerGas"] = int(tx["maxPriorityFeePerGas"] * factor)
        replacement.bundle_hash = plan.bundle_hash[:16] + "rep" + hashlib.sha3_256(
            f"{plan.bundle_hash}:{bump}".encode()
        ).hexdigest()[:12]
        return replacement
        return plan

    def cancel(self, plan: BundlePlan, sender: str = "") -> BundlePlan:
        """Отмена: self-transfer с тем же nonce и повышенной комиссией."""
        bump = (100 + self.settings.exec_replacement_bump_pct) / 100
        return BundlePlan(
            chain_id=plan.chain_id,
            target=sender or plan.sender,
            calldata_preview="0x",
            value=0,
            gas_limit=21_000,
            max_fee_per_gas_wei=int(plan.max_fee_per_gas_wei * bump),
            max_priority_fee_per_gas_wei=int(plan.max_priority_fee_per_gas_wei * bump),
            nonce=plan.nonce,
            bundle_hash=hashlib.sha3_256(f"cancel:{plan.bundle_hash}:{plan.nonce}".encode()).hexdigest(),
            txs=[],
            sender=sender or plan.sender,
            strategy=plan.strategy,
            opportunity_uid=plan.opportunity_uid,
            dry_run=plan.dry_run,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _hash_bundle(opp: Opportunity, calldata: str, nonce: int) -> str:
        """Идентификатор bundle: детерминированный хэш состава (для журнала и матчинга).

        В production хэш приходит от релиза (``eth_sendBundle`` возвращает bundleHash),
        здесь — локальный аналог для учёта в БД и отображения в UI.
        """
        material = ":".join(
            [str(opp.chain_id), opp.uid, opp.strategy, calldata, str(nonce), str(opp.block_number)]
        ).encode()
        return "0x" + hashlib.sha3_256(material).hexdigest()

    @staticmethod
    def _encode_preview(opp: Opportunity) -> str:
        """Предпросмотр calldata: селектор функции + компактные аргументы (не для отправки)."""
        payload = json.dumps(
            {
                "strategy": opp.strategy,
                "kind": opp.kind,
                "token_in": opp.token_in,
                "token_out": opp.token_out,
                "size_in": str(opp.size_in),
                "min_out": str(opp.expected_out),
                "pools": opp.pools,
                "deadline": int(time.time()) + 30,
            },
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha3_256(payload).hexdigest()
        selector = digest[:8]
        return "0x" + selector + digest[8:]
