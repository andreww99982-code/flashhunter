"""Боевая отправка сделок в блокчейн: pre-flight → подпись → отправка → подтверждение.

Модуль закрывает последний участок пути от найденной возможности до реальной
транзакции в сети. Никаких «примерно так»: каждый шаг выполняется против живого
узла и против реального контракта-исполнителя:

1. **Pre-flight** — ``eth_call`` с настоящей calldata по текущему состоянию сети.
   Если контракт ревертит (``Unprofitable``, ``DeadlineExpired``, нехватка
   ликвидности), сделка не отправляется: мы платим за газ только тогда, когда
   транзакция действительно прошла бы. Затем ``eth_estimateGas`` даёт реальный
   расход газа для расчёта экономики.
2. **Подпись** — EIP-1559 (type 2) ключом кипера; nonce берётся из сети
   (``eth_getTransactionCount`` с ``pending``), комиссии — из сети
   (``eth_maxPriorityFeePerGas``/``eth_gasPrice`` + ``baseFee`` блока), с
   ограничением сверху из настроек риска.
3. **Отправка** — сначала приватные каналы: ``eth_sendBundle`` во все включённые
   релеи (Flashbots-совместимый API с подписью тела запроса ключом кипера), затем
   при отказе всех релеев — обычная транзакция через ``eth_sendRawTransaction``.
4. **Подтверждение** — ожидание receipts: статус, фактический газ, номер блока и
   **реальная прибыль из события** ``ArbitrageExecuted``, а не из оценки.

Если ключа нет, кошелёк в режиме readonly: ``submit`` завершается отказом
``no_signer``. Исполнитель обязан считать такую сделку НЕоформленной — выдавать
успех «на бумаге» запрещено.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from flashhunter.chain import abi
from flashhunter.chain.rpc import AsyncRpcPool, RpcError, RpcUnavailable
from flashhunter.chain.signer import KeeperWallet, SignedTx, mask_address

# Минимально осмысленные чаевые (0.05 gwei): ниже этого транзакция рискует не попасть
# в блок даже при тихой сети, а узлы часто возвращают 0 вместо подсказки.
MIN_TIP_WEI = 50_000_000

# Как часто перепроверять наличие кода по адресу исполнителя (секунды)
CONTRACT_CODE_TTL_SEC = 300.0

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
# ABI контракта-исполнителя (см. contracts/src/FlashLoanArbitrage.sol)
# ---------------------------------------------------------------------- #
ARB_PARAMS_TUPLE = "(address,address,address[],address[],uint256,uint256)"
EXECUTE_ARBITRAGE_SIGNATURE = f"executeArbitrage(address,uint256,{ARB_PARAMS_TUPLE})"
EXECUTE_ARBITRAGE_SELECTOR = "0x" + keccak(text=EXECUTE_ARBITRAGE_SIGNATURE)[:4].hex()

# Событие о фактической прибыли: profit читается из лога, а не из нашей оценки
ARBITRAGE_EXECUTED_TOPIC = "0x" + keccak(
    text="ArbitrageExecuted(address,uint256,uint256,uint256)"
).hex()

# Ошибки контракта: селектор -> человекочитаемое объяснение.
# Нужны, чтобы в журнале и в UI был настоящий реверт-реason, а не «execution reverted».
CONTRACT_ERRORS: dict[str, str] = {
    "0x" + keccak(text="Unprofitable(uint256,uint256)")[:4].hex(): "прибыль ниже minProfit (Unprofitable)",
    "0x" + keccak(text="DeadlineExpired()")[:4].hex(): "истёк срок исполнения (DeadlineExpired)",
    "0x" + keccak(text="ContractPaused()")[:4].hex(): "контракт на паузе (ContractPaused)",
    "0x" + keccak(text="NotKeeper()")[:4].hex(): "ключ кипера не имеет прав (NotKeeper)",
    "0x" + keccak(text="NotOwner()")[:4].hex(): "вызов разрешён только владельцу (NotOwner)",
    "0x" + keccak(text="BadPath()")[:4].hex(): "маршрут свопа некорректен (BadPath)",
    "0x" + keccak(text="NotSelfInitiated()")[:4].hex(): "вызов не от самого контракта (NotSelfInitiated)",
    "0x" + keccak(text="NotAavePool()")[:4].hex(): "колбэк пришёл не от пула Aave (NotAavePool)",
    # Стандартные ошибки EVM/токенов — самые частые на практике
    "0x08c379a0": "revert с причиной (Error(string))",
    "0x4e487b71": "паника Solidity (Panic(uint256))",
}

# Частые реверты токенов/пулов Aave, которые мы видим как «пустой» ответ
AAVE_FLASHLOAN_HINTS: dict[str, str] = {
    "FLASHLOAN_DISABLED": "флеш-кредиты Aave выключены для этого актива",
    "INVALID_AMOUNT": "пул Aave отверг сумму займа (INVALID_AMOUNT)",
    "TRANSFER_FROM_FAILED": "перевод токена не прошёл (TRANSFER_FROM_FAILED)",
}


@dataclass(slots=True)
class ArbRoute:
    """Маршрут арбитража в терминах контракта-исполнителя."""

    buy_router: str
    sell_router: str
    buy_path: list[str]
    sell_path: list[str]
    min_profit: int      # в единицах asset (защита от проскальзывания)
    deadline: int        # unix-время: транзакция, включённая позже, ревертится

    def as_tuple(self) -> tuple[str, str, list[str], list[str], int, int]:
        """Аргументы для ABI-кодирования (порядок совпадает со структурой ArbParams)."""
        return (
            to_checksum_address(self.buy_router),
            to_checksum_address(self.sell_router),
            [to_checksum_address(t) for t in self.buy_path],
            [to_checksum_address(t) for t in self.sell_path],
            int(self.min_profit),
            int(self.deadline),
        )


@dataclass(slots=True)
class PreflightResult:
    """Результат реальной проверки транзакции против состояния сети."""

    ok: bool
    reason: str = ""
    gas_estimate: int = 0
    return_data: str = ""
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "gas_estimate": self.gas_estimate,
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass(slots=True)
class SubmitOutcome:
    """Результат отправки: что реально произошло в сети."""

    submitted: bool
    status: str                      # submitted | reverted | dropped | no_signer | rejected
    tx_hash: str = ""
    relay: str = ""
    reason: str = ""
    gas_used: int = 0
    block_number: int = 0
    profit_wei: int = 0              # реальная прибыль из события ArbitrageExecuted
    confirmations: int = 0
    latency_ms: float = 0.0
    relays_tried: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "status": self.status,
            "tx_hash": self.tx_hash,
            "relay": self.relay,
            "reason": self.reason,
            "gas_used": self.gas_used,
            "block_number": self.block_number,
            "profit_wei": str(self.profit_wei),
            "confirmations": self.confirmations,
            "latency_ms": round(self.latency_ms, 2),
            "relays_tried": self.relays_tried,
        }


def decode_revert(data: str) -> str:
    """Расшифровывает revert-данные контракта в понятное объяснение.

    Узел возвращает «сырые» байты: селектор ошибки + аргументы. Без расшифровки в
    журнале остаётся невнятное ``execution reverted``, и невозможно понять, была ли
    это наша ошибка маршрута или враждебное состояние пула.
    """
    if not data or data == "0x":
        return "revert без данных (пустой ответ контракта)"
    selector = data[:10].lower()
    name = CONTRACT_ERRORS.get(selector)
    if name is None:
        for hint in AAVE_FLASHLOAN_HINTS:
            if hint.lower() in data.lower():
                return AAVE_FLASHLOAN_HINTS[hint]
        return f"неизвестная ошибка контракта (селектор {selector})"

    payload = data[10:]
    try:
        if selector == "0x" + keccak(text="Unprofitable(uint256,uint256)")[:4].hex():
            profit, min_profit = abi_decode(["uint256", "uint256"], payload)
            return f"{name}: прибыль {profit} < минимум {min_profit}"
        if selector == "0x08c379a0":  # Error(string)
            (text,) = abi_decode(["string"], payload)
            return f"revert: {text}"
        if selector == "0x4e487b71":  # Panic(uint256)
            (code,) = abi_decode(["uint256"], payload)
            return f"паника Solidity, код {code}"
    except Exception:  # noqa: BLE001 — расшифровка не должна ломать обработку ошибки
        return name
    return name


def abi_decode(types: list[str], payload: str) -> tuple[Any, ...]:
    """Декодирует аргументы revert-ошибки (селектор уже отброшен).

    Импорт eth_abi локальный: в горячем пути (проверка возможности) расшифровка
    ошибок не нужна, а стоит импорт недёшево.
    """
    from eth_abi import decode as eth_decode

    raw = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
    return tuple(eth_decode(types, raw))


class OnChainSubmitter:
    """Отправка реальных транзакций: pre-flight, подпись, релеи, подтверждение.

    Класс сознательно не имеет «демо-режима»: единственный способ получить
    ``submitted=True`` — транзакция, которую принял узел или релей. Отсутствие
    ключа или контракта-исполнителя приводит к отказу с явной причиной.
    """

    def __init__(
        self,
        rpc_urls: Iterable[str],
        wallet: KeeperWallet,
        *,
        chain_id: int = 1,
        executor_contract: str = "",
        relays: dict[str, str] | None = None,
        gas_ceiling_gwei: float = 45.0,
        priority_fee_gwei: float = 2.0,
        receipt_timeout_sec: float = 30.0,
        confirmations: int = 1,
        routers: dict[str, str] | None = None,
    ) -> None:
        self.rpc = AsyncRpcPool(list(rpc_urls))
        self.wallet = wallet
        self.chain_id = chain_id
        self.executor_contract = (executor_contract or "").lower()
        self.relays = {name: url for name, url in (relays or {}).items() if url}
        self.gas_ceiling_gwei = float(gas_ceiling_gwei)
        self.priority_fee_gwei = float(priority_fee_gwei)
        self.receipt_timeout_sec = float(receipt_timeout_sec)
        self.confirmations = int(confirmations)
        # Адрес роутера по имени DEX: контракт работает с роутерами, а не с пулами
        self.routers = {k.lower(): v for k, v in (routers or DEFAULT_ROUTERS).items()}
        self._code_present = False
        self._code_checked_at = 0.0
        self._code_error = ""
        self.stats = {
            "preflight_ok": 0,
            "preflight_revert": 0,
            "submitted": 0,
            "confirmed": 0,
            "reverted_on_chain": 0,
            "relay_accepted": 0,
            "raw_fallback": 0,
            "no_signer": 0,
        }

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Открывает соединения с узлами."""
        await self.rpc.start()

    async def close(self) -> None:
        await self.rpc.close()

    @property
    def can_submit(self) -> bool:
        """Готов ли модуль отправить реальную транзакцию (есть ключ и адрес контракта)."""
        return self.wallet.can_sign and bool(self.executor_contract)

    def describe(self) -> dict[str, Any]:
        """Состояние модуля для UI и health-эндпоинта (без секретов)."""
        return {
            "chain_id": self.chain_id,
            "signer": mask_address(self.wallet.address) if self.wallet.can_sign else "",
            "signer_mode": self.wallet.mode,
            "executor_contract": mask_address(self.executor_contract) if self.executor_contract else "",
            "relays": sorted(self.relays),
            "raw_fallback": True,
            "stats": dict(self.stats),
        }

    # ------------------------------------------------------------------ #
    # Кодирование вызова контракта
    # ------------------------------------------------------------------ #
    def encode_execute_arbitrage(self, asset: str, amount: int, route: ArbRoute) -> str:
        """Собирает настоящую calldata для ``executeArbitrage``.

        Кодирование делает eth-abi, поэтому calldata побайтово совпадает с тем, что
        ожидает контракт: любое расхождение здесь означало бы вызов пустой функции.
        """
        encoded_params = abi_encode([ARB_PARAMS_TUPLE], [route.as_tuple()])
        return EXECUTE_ARBITRAGE_SELECTOR + abi_encode(
            ["address", "uint256", "bytes"], [to_checksum_address(asset), int(amount), encoded_params]
        ).hex()

    def router_for(self, dex: str) -> str:
        """Адрес роутера для стратегии (контракт торгует через роутеры).

        Если стратегия работает с пулом напрямую (JIT/backrun), используется
        роутер соответствующей DEX: контракт сам решает, как провести своп.
        """
        key = (dex or "").lower()
        for name, address in self.routers.items():
            if name == key or name in key or key in name:
                return address
        return ""

    # ------------------------------------------------------------------ #
    # Газ и комиссии из сети
    # ------------------------------------------------------------------ #
    async def fee_params(self) -> tuple[int, int, float]:
        """Возвращает (maxFeePerGas, maxPriorityFeePerGas, baseFeeGwei) из сети.

        Приоритет — реальным подсказкам узла: ``eth_maxPriorityFeePerGas`` и baseFee
        последнего блока. Всё, что выше потолка из настроек риска, отклоняется на
        уровне исполнителя — мы не переплачиваем в перегруженной сети.
        """
        block_data, tip_data, gas_price_data = await asyncio.gather(
            self.rpc.call("eth_getBlockByNumber", ["latest", False]),
            self._safe_call("eth_maxPriorityFeePerGas", []),
            self._safe_call("eth_gasPrice", []),
        )
        base_fee = int(block_data.get("baseFeePerGas", "0x0"), 16) if isinstance(block_data, dict) else 0
        tip = int(tip_data, 16) if isinstance(tip_data, str) and tip_data.startswith("0x") else 0
        gas_price = int(gas_price_data, 16) if isinstance(gas_price_data, str) and gas_price_data.startswith("0x") else 0

        # Чаевые: берём подсказку узла, но не ниже MIN_TIP и не выше настроенного потолка.
        # Ноль или 1 wei от публичного узла — это отсутствие данных, а не «дешёвая» сеть.
        configured_tip = int(self.priority_fee_gwei * 1e9)
        tip = max(tip, MIN_TIP_WEI)
        if configured_tip > 0:
            tip = min(tip, configured_tip)
        if base_fee <= 0 and gas_price > 0:
            base_fee = max(gas_price - tip, 1)

        # Запас 20% к base fee: блоки дорожают, а реверт по «underpriced» нас не устраивает
        max_fee = int(base_fee * 1.2) + tip
        ceiling = int(self.gas_ceiling_gwei * 1e9)
        if max_fee > ceiling:
            raise ValueError(
                f"комиссия {max_fee / 1e9:.1f} gwei превышает потолок {self.gas_ceiling_gwei:.1f} gwei"
            )
        return max_fee, tip, base_fee / 1e9

    async def _safe_call(self, method: str, params: list[Any]) -> Any:
        """Вызов, отсутствие поддержки которого не должно ломать расчёт комиссий."""
        try:
            return await self.rpc.call(method, params)
        except Exception as exc:  # noqa: BLE001
            log.debug("RPC %s недоступен: %s", method, exc)
            return None

    async def nonce(self) -> int:
        """Nonce с учётом pending-транзакций: иначе подпись «застрянет» в очереди."""
        if not self.wallet.address:
            raise RuntimeError("нет адреса кипера: невозможно получить nonce")
        raw = await self.rpc.call("eth_getTransactionCount", [self.wallet.address, "pending"])
        return int(raw, 16) if isinstance(raw, str) else int(raw)

    async def balance_wei(self) -> int:
        """Баланс кипера в сети — для контроля, хватает ли газа."""
        if not self.wallet.address:
            return 0
        raw = await self.rpc.call("eth_getBalance", [self.wallet.address, "latest"])
        return int(raw, 16) if isinstance(raw, str) else int(raw)

    # ------------------------------------------------------------------ #
    # Pre-flight
    # ------------------------------------------------------------------ #
    async def preflight(self, asset: str, amount: int, route: ArbRoute, *, sender: str = "") -> PreflightResult:
        """Прогоняет транзакцию против текущего состояния сети ДО подписи.

        ``eth_call`` — это тот же EVM-вызов, что и настоящая транзакция, только без
        списания газа: узел покажет и результат, и revert. Это единственный честный
        способ узнать, пройдёт ли сделка, не заплатив за неудачную попытку.
        """
        started = time.perf_counter()
        caller = sender or self.wallet.address
        if not caller or not self.executor_contract:
            return PreflightResult(
                ok=False,
                reason="preflight невозможен: не задан адрес контракта или кипера",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # Проверка «контракт вообще существует»: eth_call к адресу без кода возвращает
        # успех и пустой ответ — без этой проверки мы бы «успешно» проверяли вызов
        # в пустоту и отправляли транзакцию, которая ничего не делает.
        deployed, code_error = await self._contract_deployed()
        if not deployed:
            return PreflightResult(
                ok=False,
                reason=code_error,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        calldata = self.encode_execute_arbitrage(asset, amount, route)
        tx = {"from": caller, "to": self.executor_contract, "data": calldata, "value": "0x0"}

        try:
            result = await self.rpc.call("eth_call", [tx, "latest"])
        except RpcError as exc:
            # Реверт — ожидаемый штатный результат проверки: сеть говорит «сейчас бы
            # не прошло». Разбираем причину и НЕ отправляем транзакцию (газ не тратим).
            self.stats["preflight_revert"] += 1
            return PreflightResult(
                ok=False,
                reason=self._revert_reason(exc),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except RpcUnavailable as exc:
            self.stats["preflight_revert"] += 1
            return PreflightResult(
                ok=False,
                reason=self._explain_rpc_error(str(exc)),
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # eth_estimateGas даёт реальный расход: именно его мы оплачиваем
        gas_estimate = 0
        try:
            gas_raw = await self.rpc.call("eth_estimateGas", [tx])
            gas_estimate = int(gas_raw, 16) if isinstance(gas_raw, str) else int(gas_raw)
        except RpcError as exc:
            self.stats["preflight_revert"] += 1
            return PreflightResult(
                ok=False,
                reason=self._revert_reason(exc),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except RpcUnavailable as exc:
            self.stats["preflight_revert"] += 1
            return PreflightResult(
                ok=False,
                reason=self._explain_rpc_error(str(exc)),
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # Хватает ли киперу ETH на оплату газа: транзакция без баланса не попадёт в блок,
        # и лучше узнать об этом до подписи, чем после отправки.
        if self.wallet.can_sign:
            try:
                max_fee, _tip, _base = await self.fee_params()
                need = int(gas_estimate) * max_fee
                balance = await self.balance_wei()
                if balance < need:
                    self.stats["preflight_revert"] += 1
                    return PreflightResult(
                        ok=False,
                        reason=(
                            f"недостаточно ETH на газ: нужно {need / 1e18:.6f}, "
                            f"на кошельке {balance / 1e18:.6f} ETH ({mask_address(self.wallet.address)})"
                        ),
                        gas_estimate=gas_estimate,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
            except Exception as exc:  # noqa: BLE001 — не превращаем контроль в отказ
                log.debug("проверка баланса кипера не удалась: %s", exc)

        self.stats["preflight_ok"] += 1
        return PreflightResult(
            ok=True,
            gas_estimate=gas_estimate,
            return_data=str(result) if isinstance(result, str) else "",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _contract_deployed(self) -> tuple[bool, str]:
        """Проверяет, что по адресу контракта-исполнителя действительно есть код.

        Результат кэшируется на CONTRACT_CODE_TTL_SEC: код контракта меняется только
        при переотправке (selfdestruct в EVM с 2024 г. практически не применяется),
        а лишние запросы к публичным узлам стоят латентности.
        """
        if not self.executor_contract:
            return False, "не задан адрес контракта-исполнителя (FLASHLOAN_ARBITRAGE_ADDRESS)"
        now = time.time()
        if self._code_checked_at and (now - self._code_checked_at) < CONTRACT_CODE_TTL_SEC:
            return self._code_present, "" if self._code_present else self._code_error
        try:
            code = await self.rpc.call("eth_getCode", [self.executor_contract, "latest"])
        except Exception as exc:  # noqa: BLE001
            return False, f"не удалось получить код контракта: {exc}"
        present = isinstance(code, str) and code not in ("0x", "", "0x0")
        self._code_present = present
        self._code_checked_at = now
        self._code_error = (
            ""
            if present
            else f"по адресу {mask_address(self.executor_contract)} нет кода контракта: "
                 f"исполнитель не развёрнут (проверьте FLASHLOAN_ARBITRAGE_ADDRESS и scripts/deploy-local.sh)"
        )
        if not present:
            log.error("%s", self._code_error)
        return present, self._code_error

    @staticmethod
    def _revert_reason(exc: RpcError) -> str:
        """Достаёт из ошибки узла revert-данные контракта и переводит их на человеческий.

        Узлы кладут одни и те же байты в разные места (``data``, ``data.data``, текст),
        поэтому проверяем все варианты — иначе в журнале останется «execution reverted»
        без понимания, что именно не сошлось.
        """
        payload = exc.payload
        candidates: list[str] = []
        if isinstance(payload, str):
            candidates.append(payload)
        elif isinstance(payload, dict):
            for key in ("data", "return", "result"):
                value = payload.get(key)
                if isinstance(value, str):
                    candidates.append(value)
                elif isinstance(value, dict):
                    nested = value.get("data")
                    if isinstance(nested, str):
                        candidates.append(nested)
        for candidate in candidates:
            if candidate.startswith("0x") and len(candidate) >= 10:
                return decode_revert(candidate)
        text = f"{exc.message}"
        if text and len(candidates) == 0 and "revert" in text.lower():
            return f"контракт отверг вызов: {text[:160]}"
        return OnChainSubmitter._explain_rpc_error(str(exc))

    @staticmethod
    def _explain_rpc_error(message: str) -> str:
        """Достаёт revert-данные из текста ошибки RPC и переводит их на человеческий."""
        lowered = message.lower()
        marker = lowered.find("data")
        if "revert" in lowered or "data" in lowered:
            for token in message.replace('"', " ").replace(",", " ").split():
                if token.startswith("0x") and len(token) >= 10:
                    return decode_revert(token)
        return f"сеть отвергла вызов до отправки: {message[:180]}"

    # ------------------------------------------------------------------ #
    # Отправка
    # ------------------------------------------------------------------ #
    async def submit(
        self,
        *,
        asset: str,
        amount: int,
        route: ArbRoute,
        gas_limit: int,
        block_number: int | None = None,
        replacement_of: SignedTx | None = None,
        bump_pct: int = 15,
    ) -> SubmitOutcome:
        """Подписывает и отправляет транзакцию, затем ждёт подтверждения.

        Порядок: релеи (приватный канал, защита от «копирования» сделки другими
        ботами) → при отказе всех релеев обычная отправка через узел. Прибыль
        берётся из события контракта, а не из нашей модели.
        """
        started = time.perf_counter()
        if not self.wallet.can_sign:
            self.stats["no_signer"] += 1
            return SubmitOutcome(
                submitted=False,
                status="no_signer",
                reason="нет ключа кипера: отправка невозможна (режим readonly)",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if not self.executor_contract:
            return SubmitOutcome(
                submitted=False,
                status="rejected",
                reason="не задан адрес контракта-исполнителя (FLASHLOAN_ARBITRAGE_ADDRESS)",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            max_fee, tip, base_fee_gwei = await self.fee_params()
            nonce = await self.nonce()
            self.wallet.set_nonce(nonce)
            calldata = self.encode_execute_arbitrage(asset, amount, route)
            if replacement_of is not None:
                # Замена «зависшей» транзакции: тот же nonce и заведомо большая комиссия
                max_fee = int(replacement_of.max_fee_per_gas * (1 + bump_pct / 100))
                tip = int(replacement_of.max_priority_fee_per_gas * (1 + bump_pct / 100))
                nonce = replacement_of.nonce
                self.wallet.set_nonce(nonce)
            signed = self.wallet.sign_arbitrage(
                to=self.executor_contract,
                calldata=calldata,
                gas=int(gas_limit),
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=tip,
                nonce=nonce,
                chain_id=self.chain_id,
            )
        except Exception as exc:  # noqa: BLE001 — подпись и сеть могут отказать по-разному
            log.error("подготовка транзакции не удалась: %s", exc)
            return SubmitOutcome(
                submitted=False,
                status="rejected",
                reason=f"подготовка транзакции не удалась: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        log.info(
            "транзакция подписана: nonce=%d gas=%d maxFee=%.2f gwei tip=%.2f gwei baseFee=%.2f gwei hash=%s",
            nonce, signed.gas, max_fee / 1e9, tip / 1e9, base_fee_gwei, signed.tx_hash,
        )

        relay_name, relay_error = await self._send_via_relays(signed, block_number)
        if relay_name:
            self.stats["relay_accepted"] += 1
        else:
            # Все релеи отказали — идём обычным путём через узел, чтобы сделка не потерялась
            raw_ok, raw_error = await self._send_raw(signed)
            if raw_ok:
                relay_name = "public_rpc"
                self.stats["raw_fallback"] += 1
            else:
                self.stats["submitted"] += 1  # попытка была, результат — отказ
                details = []
                if relay_error:
                    details.append(f"релеи: {relay_error}")
                if raw_error:
                    details.append(f"узел: {raw_error}")
                return SubmitOutcome(
                    submitted=False,
                    status="dropped",
                    tx_hash=signed.tx_hash,
                    reason=("транзакцию не принял ни один канал — " + "; ".join(details))[:400],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )

        self.stats["submitted"] += 1
        outcome = await self.wait_receipt(signed.tx_hash, fallback_reason="")
        outcome.relay = relay_name
        outcome.tx_hash = signed.tx_hash
        outcome.latency_ms = (time.perf_counter() - started) * 1000
        return outcome

    async def _send_via_relays(self, signed: SignedTx, block_number: int | None) -> tuple[str, str]:
        """Отправка bundle во все включённые релеи. Возвращает (имя релея, ошибка).

        Для Flashbots-совместимых релеев тело запроса подписывается ключом кипера:
        без подписи они отвечают отказом, а неподписанный bundle — это уже не
        приватный канал.
        """
        if not self.relays:
            return "", "релеи не настроены"

        import httpx

        target_block = hex((block_number or 0) + 1) if block_number else "0x0"
        errors: list[str] = []
        client = httpx.AsyncClient(timeout=httpx.Timeout(2.0), headers={"Content-Type": "application/json"})
        try:
            for name, url in self.relays.items():
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_sendBundle",
                    "params": [
                        {
                            "txs": [signed.raw],
                            "blockNumber": target_block,
                            "minTimestamp": 0,
                            "maxTimestamp": int(time.time()) + 30,
                        }
                    ],
                }
                headers = dict(client.headers)
                # Подписываем и отправляем одни и те же байты: расхождение сериализации
                # (пробелы в JSON) делает подпись недействительной.
                import json as _json

                body_bytes = _json.dumps(payload, separators=(",", ":")).encode()
                signature = self._flashbots_signature(body_bytes)
                if signature:
                    headers["X-Flashbots-Signature"] = signature
                try:
                    response = await client.post(url, content=body_bytes, headers=headers)
                    body: Any
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"raw": response.text[:160]}
                    if response.status_code == 200 and not (isinstance(body, dict) and body.get("error")):
                        log.info("bundle принят релеем %s", name)
                        return name, ""
                    errors.append(f"{name}: {str(body)[:120]}")
                except Exception as exc:  # noqa: BLE001 — один релей не должен ронять остальные
                    errors.append(f"{name}: {exc}")
        finally:
            await client.aclose()
        return "", "; ".join(errors)[:300]

    def _flashbots_signature(self, body: bytes) -> str:
        """Подпись тела запроса для Flashbots: ``адрес:подпись``.

        Схема релея (проверена на живом relay.flashbots.net): берётся keccak256 тела
        запроса, затем EIP-191 (personal_sign) по его **hex-строке**. Подпись прямо
        по телу релей отвергает как «invalid flashbots signature» — это неочевидная
        деталь, из-за которой приватный канал молча не работает.
        """
        if not self.wallet.can_sign or self.wallet.account is None:
            return ""
        digest = "0x" + keccak(body).hex()
        message = Account.sign_message(
            _encode_defunct(text=digest), private_key=self.wallet.account.key
        )
        signature = message.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        return f"{self.wallet.address}:{signature}"

    async def _send_raw(self, signed: SignedTx) -> tuple[bool, str]:
        """Резервный путь: обычная отправка сырой транзакции через узел."""
        try:
            result = await self.rpc.call("eth_sendRawTransaction", [signed.raw])
            log.info("транзакция отправлена через узел: %s", result)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            log.warning("eth_sendRawTransaction отверг транзакцию: %s", exc)
            return False, str(exc)[:200]

    # ------------------------------------------------------------------ #
    # Подтверждение
    # ------------------------------------------------------------------ #
    async def wait_receipt(self, tx_hash: str, *, fallback_reason: str = "") -> SubmitOutcome:
        """Ждёт подтверждения и читает фактическую прибыль из события контракта."""
        started = time.perf_counter()
        deadline = time.time() + self.receipt_timeout_sec
        receipt: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                result = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            except Exception as exc:  # noqa: BLE001
                log.debug("receipt %s: %s", tx_hash, exc)
                result = None
            if isinstance(result, dict) and result:
                receipt = result
                break
            await asyncio.sleep(1.0)

        if receipt is None:
            return SubmitOutcome(
                submitted=True,
                status="submitted",
                tx_hash=tx_hash,
                reason="транзакция отправлена, подтверждение не получено в отведённое время",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        status_ok = int(receipt.get("status", "0x0"), 16) == 1
        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        block_number = int(receipt.get("blockNumber", "0x0"), 16)
        profit = self._profit_from_logs(receipt.get("logs", []) or [])

        if not status_ok:
            self.stats["reverted_on_chain"] += 1
            return SubmitOutcome(
                submitted=True,
                status="reverted",
                tx_hash=tx_hash,
                gas_used=gas_used,
                block_number=block_number,
                reason=fallback_reason or "транзакция включена в блок с ревертом (газ потрачен)",
                confirmations=1,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        self.stats["confirmed"] += 1
        return SubmitOutcome(
            submitted=True,
            status="confirmed",
            tx_hash=tx_hash,
            gas_used=gas_used,
            block_number=block_number,
            profit_wei=profit,
            confirmations=1,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _profit_from_logs(logs: list[dict[str, Any]]) -> int:
        """Читает прибыль из события ``ArbitrageExecuted`` в receipt.

        Прибыль считает контракт — это единственное число, которому можно верить:
        наша модель могла не учесть проскальзывание и комиссии.
        """
        for entry in logs:
            topics = entry.get("topics") or []
            if topics and str(topics[0]).lower() == ARBITRAGE_EXECUTED_TOPIC:
                data = str(entry.get("data", "0x"))
                try:
                    words = abi.words(data)
                except ValueError:
                    continue
                # (indexed asset, borrowed, premium, profit) → profit — последнее слово
                if words:
                    return int(words[-1])
        return 0


def _encode_defunct(*, text: str = "", primitive: bytes | None = None) -> Any:
    """EIP-191 префикс сообщения (подпись тела запроса релею).

    Обёртка над eth-account: принимает либо текст (hex-дайджест), либо сырые байты.
    """
    from eth_account.messages import encode_defunct

    if primitive is not None:
        return encode_defunct(primitive=primitive)
    return encode_defunct(text=text)


# Роутеры mainnet: контракт-исполнитель работает через них, а не через пулы
DEFAULT_ROUTERS: dict[str, str] = {
    "uniswap_v2": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "uniswap_v3": "0xE592427A0AEce92De3Edee1F18E0157C05861564",   # SwapRouter
    "uniswap_v3_2": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  # SwapRouter02
    "sushiswap": "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
    "curve": "0x99a58482BD75cbab83b27EC03CA68fF489b5788f",         # CurveRouter
    "aave_v3": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
}
