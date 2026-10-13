"""Подпись реальных транзакций (EIP-1559) и управление ключами горячего кошелька.

Ключи берутся из окружения или из файла с правами 0400 (в проде файл создаёт
Ansible из AWS Secrets Manager). В открытом виде ключ никогда не логируется и
не попадает в БД: в API хранится только адрес и маска.

Если ключа нет, кошелёк работает в режиме ``readonly``: транзакции не подписываются,
и исполнитель обязан отказаться от отправки, а не «посчитать» сделку успешной.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address

log = logging.getLogger(__name__)

PRIVATE_KEY_PATTERN = re.compile(r"^0x[0-9a-fA-F]{64}$")

# Идентификаторы сетей для EIP-155 (реальные, а не выдуманные)
CHAIN_IDS = {
    "mainnet": 1,
    "arbitrum": 42161,
    "base": 8453,
    "optimism": 10,
    "polygon": 137,
    "bsc": 56,
    "avalanche": 43114,
    "anvil": 31337,
}


def mask_address(address: str) -> str:
    """Маска адреса для UI: 0x1234…cdef."""
    if len(address) < 12:
        return address
    return f"{address[:6]}…{address[-4:]}"


@dataclass(slots=True)
class SignedTx:
    """Подписанная транзакция, готовая к отправке в сеть или в bundle."""

    raw: str                 # 0x-hex сырой RLP
    tx_hash: str             # keccak подписанной транзакции
    nonce: int
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    chain_id: int
    to: str
    value: int = 0

    def as_rpc(self) -> dict[str, Any]:
        """Транзакция в формате eth_sendBundle / eth_call."""
        return {
            "to": self.to,
            "data": "",  # calldata уже внутри raw; для релея важен именно raw
            "gas": hex(self.gas),
            "value": hex(self.value),
            "maxFeePerGas": hex(self.max_fee_per_gas),
            "maxPriorityFeePerGas": hex(self.max_priority_fee_per_gas),
            "nonce": hex(self.nonce),
            "chainId": hex(self.chain_id),
        }


class KeeperWallet:
    """Горячий кошелёк кипера: подпись транзакций и nonce-менеджмент.

    Держит ключ только в памяти процесса. Публичный интерфейс не возвращает
    приватный ключ ни в каком виде — только адрес и подписанные транзакции.
    """

    def __init__(self, private_key: str = "", *, chain_id: int = 1) -> None:
        self.chain_id = chain_id
        self.account: LocalAccount | None = None
        self.address: str = ""
        self.mode: str = "readonly"
        self._local_nonce: int | None = None

        key = (private_key or "").strip()
        if key and not PRIVATE_KEY_PATTERN.match(key):
            raise ValueError("приватный ключ должен быть 0x + 64 hex-символа (EVM)")
        if key:
            self.account = Account.from_key(key)
            self.address = to_checksum_address(self.account.address)
            self.mode = "signer"
            log.info("Keeper-кошелёк загружен: %s (сеть %d)", mask_address(self.address), chain_id)
        else:
            log.warning(
                "Keeper-ключ не задан: работаем в режиме readonly. "
                "Транзакции подписываться не будут — отправка в сеть невозможна."
            )

    # ------------------------------------------------------------------ #
    @property
    def can_sign(self) -> bool:
        """Может ли кошелёк подписывать транзакции."""
        return self.account is not None

    def nonce(self) -> int | None:
        """Локально удерживаемый nonce (обновляется из сети перед подписью)."""
        return self._local_nonce

    def set_nonce(self, value: int) -> None:
        """Синхронизация nonce с сетью (eth_getTransactionCount pending)."""
        self._local_nonce = int(value)

    def next_nonce(self) -> int:
        """Следующий nonce: используется для пакетных отправок в одном блоке."""
        if self._local_nonce is None:
            raise RuntimeError("nonce не синхронизирован с сетью: вызовите set_nonce() перед подписью")
        value = self._local_nonce
        self._local_nonce += 1
        return value

    # ------------------------------------------------------------------ #
    def sign_arbitrage(
        self,
        *,
        to: str,
        calldata: str,
        gas: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        nonce: int | None = None,
        value: int = 0,
        chain_id: int | None = None,
    ) -> SignedTx:
        """Подписывает транзакцию вызова контракта-исполнителя (EIP-1559, type 2)."""
        if self.account is None:
            raise RuntimeError("нет ключа: подпись недоступна (режим readonly)")

        tx = {
            "to": to_checksum_address(to),
            "data": calldata,
            "value": value,
            "gas": int(gas),
            "maxFeePerGas": int(max_fee_per_gas),
            "maxPriorityFeePerGas": int(max_priority_fee_per_gas),
            "nonce": int(self.next_nonce() if nonce is None else nonce),
            "chainId": int(chain_id if chain_id is not None else self.chain_id),
            "type": 2,
        }
        signed = self.account.sign_transaction(tx)
        raw = signed.raw_transaction.hex()
        raw = raw if raw.startswith("0x") else "0x" + raw
        tx_hash = signed.hash.hex()
        tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash

        return SignedTx(
            raw=raw,
            tx_hash=tx_hash,
            nonce=tx["nonce"],
            gas=tx["gas"],
            max_fee_per_gas=tx["maxFeePerGas"],
            max_priority_fee_per_gas=tx["maxPriorityFeePerGas"],
            chain_id=tx["chainId"],
            to=tx["to"],
            value=tx["value"],
        )

    def sign_cancel(self, *, gas: int = 21_000, max_fee_per_gas: int, nonce: int, chain_id: int | None = None) -> SignedTx:
        """Подписывает отмену: self-transfer с тем же nonce (освобождает nonce в сети)."""
        return self.sign_arbitrage(
            to=self.address,
            calldata="0x",
            gas=gas,
            max_fee_per_gas=int(max_fee_per_gas * 1.15),  # отмена обязана быть дороже исходной
            max_priority_fee_per_gas=int(max_fee_per_gas * 1.15),
            nonce=nonce,
            chain_id=chain_id,
        )


def load_keeper_key(env_value: str = "", key_file: str = "") -> str:
    """Ищет ключ кипера: переменная окружения → файл (0400) → keystore JSON.

    Файл может содержать как сырой ключ, так и JSON keystore (формат geth/Anvil)
    вместе с паролем в переменной ``KEEPER_KEYSTORE_PASSWORD``.
    """
    if env_value:
        return env_value.strip()

    if not key_file:
        return ""

    path = pathlib.Path(key_file)
    if not path.exists():
        log.warning("файл ключа %s не найден", key_file)
        return ""

    # Прод-требование: ключ не должен быть доступен группе и остальным
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning("права на %s = %o: ключ доступен группе/остальным (нужно 0400)", key_file, mode)
    except OSError:
        pass

    content = path.read_text(encoding="utf-8").strip()
    if content.startswith("{"):
        # Keystore JSON: расшифровываем паролем из окружения (пароль не логируем)
        password = os.environ.get("KEEPER_KEYSTORE_PASSWORD", "")
        if not password:
            log.error("keystore найден, но KEEPER_KEYSTORE_PASSWORD не задан — ключ не загружен")
            return ""
        try:
            account = Account.from_key(Account.decrypt(json.loads(content), password))
            return account.key.hex()
        except Exception as exc:  # noqa: BLE001 — расшифровка может не удаться
            log.error("не удалось расшифровать keystore: %s", type(exc).__name__)
            return ""
    return content
