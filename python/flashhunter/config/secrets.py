"""Работа с секретами: AES-GCM локально, Vault transit при наличии.

Порядок разрешения:
    1) HashiCorp Vault (VAULT_ADDR + VAULT_TOKEN) — transit engine;
    2) локальный ключ установки рядом с настройками панели (AES-256-GCM).

Новые локальные шифротексты используют ``local:v2``; ``local:v1`` читается для миграции.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from flashhunter.config.panel_config import settings_file_path

log = logging.getLogger(__name__)


def _legacy_local_key() -> bytes:
    """Derives the key used by legacy local:v1 ciphertexts."""
    material = os.environ.get("FLASHHUNTER_LOCAL_KEY", "") or os.environ.get("JWT_SECRET", "dev-local-key")
    return hashlib.sha3_256(material.encode()).digest()


def _local_key() -> bytes:
    """Loads or creates a stable machine key independent of rotatable JWT credentials."""
    path = settings_file_path().with_suffix(".key")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_bytes()
    else:
        key = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
            key_file.flush()
            os.fsync(key_file.fileno())
    if len(key) != 32:
        raise ValueError("invalid local encryption key; restore the matching key file")
    if os.name == "posix":
        path.chmod(0o600)
    return key


def _xor_stream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Legacy key stream retained only to decrypt local:v1 values.

    New secrets use AES-GCM; this function exists only for compatibility with
    previously encrypted values.
    """
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out, strict=False))


def encrypt_secret(plaintext: str) -> str:
    """Encrypts secrets with AES-GCM and a persistent per-installation key."""
    if os.environ.get("VAULT_ADDR"):
        return _vault_encrypt(plaintext)
    key = _local_key()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return f"local:v2:{base64.b64encode(nonce).decode()}:{base64.b64encode(ciphertext).decode()}"


def decrypt_secret(payload: str) -> str:
    """Расшифровывает секрет и проверяет тег целостности."""
    if payload.startswith("vault:"):
        return _vault_decrypt(payload)
    if payload.startswith("local:v2:"):
        try:
            _, _, nonce_b64, ciphertext_b64 = payload.split(":")
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ciphertext_b64)
            return AESGCM(_local_key()).decrypt(nonce, ciphertext, None).decode()
        except (InvalidTag, ValueError, TypeError) as exc:
            raise ValueError("secret decryption failed (wrong key or tampered payload)") from exc
    try:
        _, version, nonce_b64, ct_b64, tag_b64 = payload.split(":")
        if version != "v1":
            raise ValueError(f"unsupported version {version}")
        nonce = base64.b64decode(nonce_b64)
        ciphertext = base64.b64decode(ct_b64)
        tag = base64.b64decode(tag_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed secret payload") from exc

    key = _legacy_local_key()
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
