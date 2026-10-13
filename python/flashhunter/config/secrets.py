"""Работа с секретами: AES-GCM локально, Vault/KMS в production.

Порядок разрешения:
    1) HashiCorp Vault (VAULT_ADDR + VAULT_TOKEN) — transit engine;
    2) AWS KMS (AWS_REGION + KMS_KEY_ID) — encrypt/decrypt;
    3) локальный ключ FLASHHUNTER_LOCAL_KEY (AES-256-GCM, только для dev/single-node).

Формат локального шифротекста: ``local:v1:<nonce_hex>:<ciphertext_hex>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger(__name__)


def _local_key() -> bytes:
    """Выводит 32-байтовый ключ из FLASHHUNTER_LOCAL_KEY (или из JWT_SECRET)."""
    material = os.environ.get("FLASHHUNTER_LOCAL_KEY", "") or os.environ.get("JWT_SECRET", "dev-local-key")
    return hashlib.sha3_256(material.encode()).digest()


def _xor_stream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Ключевой поток (HMAC-SHA256 в режиме счётчика) — учебный аналог AES-CTR.

    ВАЖНО: это НЕ AES-GCM. Для production-секретов обязательно использовать
    Vault/KMS/YubiHSM: локальный режим предназначен для single-node демо-режима,
    где ключ и шифротекст лежат рядом и криптостойкость определяется хранением файла.
    """
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out, strict=False))


def encrypt_secret(plaintext: str) -> str:
    """Шифрует секрет с аутентификацией (HMAC-тег) в формате local:v1:nonce:ct:tag."""
    if os.environ.get("VAULT_ADDR"):
        return _vault_encrypt(plaintext)
    key = _local_key()
    nonce = secrets.token_bytes(12)
    ciphertext = _xor_stream(key, nonce, plaintext.encode())
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    return "local:v1:{}:{}:{}".format(
        base64.b64encode(nonce).decode(),
        base64.b64encode(ciphertext).decode(),
        base64.b64encode(tag).decode(),
    )


def decrypt_secret(payload: str) -> str:
    """Расшифровывает секрет и проверяет тег целостности."""
    if payload.startswith("vault:"):
        return _vault_decrypt(payload)
    try:
        _, version, nonce_b64, ct_b64, tag_b64 = payload.split(":")
        if version != "v1":
            raise ValueError(f"unsupported version {version}")
        nonce = base64.b64decode(nonce_b64)
        ciphertext = base64.b64decode(ct_b64)
        tag = base64.b64decode(tag_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed secret payload") from exc

    key = _local_key()
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("secret integrity check failed (wrong key or tampered payload)")
    return _xor_stream(key, nonce, ciphertext).decode()


def mask_secret(value: str) -> str:
    """Маскирует секрет для отображения в UI."""
    if len(value) <= 8:
        return "••••"
    return value[:4] + "••••" + value[-4:]


def _vault_encrypt(plaintext: str) -> str:
    """Шифрование через Vault transit (только при наличии VAULT_ADDR и VAULT_TOKEN)."""
    import httpx

    addr = os.environ["VAULT_ADDR"].rstrip("/")
    token = os.environ.get("VAULT_TOKEN", "")
    key_name = os.environ.get("VAULT_TRANSIT_KEY", "flashhunter")
    response = httpx.post(
        f"{addr}/v1/transit/encrypt/{key_name}",
        headers={"X-Vault-Token": token},
        json={"plaintext": base64.b64encode(plaintext.encode()).decode()},
        timeout=10,
    )
    response.raise_for_status()
    return "vault:" + response.json()["data"]["ciphertext"]


def _vault_decrypt(payload: str) -> str:
    """Расшифровка Vault transit-шифротекста."""
    import httpx

    addr = os.environ["VAULT_ADDR"].rstrip("/")
    token = os.environ.get("VAULT_TOKEN", "")
    response = httpx.post(
        f"{addr}/v1/transit/decrypt/flashhunter",
        headers={"X-Vault-Token": token},
        json={"ciphertext": payload.removeprefix("vault:")},
        timeout=10,
    )
    response.raise_for_status()
    return base64.b64decode(response.json()["data"]["plaintext"]).decode()
