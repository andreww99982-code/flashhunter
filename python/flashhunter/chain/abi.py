"""Кодирование вызовов и разбор данных EVM без внешних ABI-файлов.

Селекторы считаются keccak-ом от сигнатуры прямо в рантайме: так невозможно
опечататься в хардкод-константе и сразу видно, какой именно метод вызывается.
Все функции здесь работают с настоящими байтами calldata — то же, что уходит в сеть.
"""

from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

# --------------------------------------------------------------------------- #
# Кодирование
# --------------------------------------------------------------------------- #


def selector(signature: str) -> str:
    """4-байтовый селектор метода по сигнатуре, hex с 0x."""
    return "0x" + keccak(text=signature)[:4].hex()


def encode_call(signature: str, types: list[str], args: list[Any]) -> str:
    """Полный calldata: селектор + ABI-аргументы."""
    return selector(signature) + abi_encode(types, args).hex()


def pad_address(address: str) -> str:
    """Адрес как 32-байтовое слово ABI (без 0x-префикса)."""
    return address.lower().replace("0x", "").rjust(64, "0")


def topic_for(signature: str) -> str:
    """Топик события (keccak полной сигнатуры), 32 байта hex с 0x."""
    return "0x" + keccak(text=signature).hex()


# --------------------------------------------------------------------------- #
# Разбор результатов
# --------------------------------------------------------------------------- #


def words(data: str) -> list[int]:
    """Результат eth_call → список 32-байтовых слов как int."""
    if not data or data in {"0x", "0x0"}:
        return []
    body = data[2:] if data.startswith("0x") else data
    return [int(body[i : i + 64], 16) for i in range(0, len(body) - (len(body) % 64), 64)]


def word(data: str, index: int) -> int:
    """Одно слово результата по индексу (0 — первое)."""
    values = words(data)
    if index >= len(values):
        raise ValueError(f"в результате {len(values)} слов, запрошено слово #{index}")
    return values[index]


def address_at(data: str, index: int = 0) -> str:
    """Адрес из 32-байтового слова (младшие 20 байт), с checksum."""
    return to_checksum_address(hex(word(data, index) & (2**160 - 1)))


def to_int(value: Any) -> int:
    """Приведение hex/int/str к int (узел отдаёт hex-строки)."""
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value, "big")
    text = str(value)
    return int(text, 16) if text.startswith("0x") else int(text)


def decode_symbol(data: str) -> str:
    """symbol() токена: поддерживает string и bytes32 (у MKR/USDC бывает по-разному)."""
    if not data or data == "0x":
        return ""
    body = data[2:]
    if len(body) == 64:  # bytes32
        raw = bytes.fromhex(body)
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
    try:
        (value,) = abi_decode(["string"], bytes.fromhex(body))
        return str(value)
    except Exception:  # noqa: BLE001 — некоторые токены ломают ABI
        return bytes.fromhex(body[:64]).rstrip(b"\x00").decode("utf-8", errors="ignore")


def decode_latest_round(data: str) -> tuple[int, int]:
    """Chainlink лatestRoundData → (answer, updated_at)."""
    values = words(data)
    if len(values) < 5:
        raise ValueError("некорректный ответ latestRoundData")
    return values[1], values[3]


def decode_string_array(data: str) -> list[str]:
    """Разбор массива адресов из результата (getAmountsOut: uint256[])."""
    values = words(data)
    if not values:
        return []
    length = values[0]
    return [hex(values[1 + index]) for index in range(min(int(length), len(values) - 1))]
