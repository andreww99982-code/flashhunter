"""Аутентификация: bcrypt-пароли, JWT-сессии, TOTP (2FA), хэш токена для отзыва.

Требования ТЗ п.10: bcrypt, JWT, 2FA для критических действий, rate limiting (см. api/ratelimit.py).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
import pyotp
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flashhunter.config.settings import get_settings
from flashhunter.db.models import Session as UserSession
from flashhunter.db.models import User
from flashhunter.db.models import utcnow
from flashhunter.db.session import get_db

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------
BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    """bcrypt-хэш пароля (cost=12). Возвращает строку для колонки ``users.password_hash``."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Проверка пароля; устойчиво к некорректным хэшам (constant-time)."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def password_strength_ok(password: str) -> tuple[bool, str]:
    """Минимальная политика пароля для смены admin/admin."""
    if len(password) < 12:
        return False, "пароль должен быть не короче 12 символов"
    if password.lower() in {"admin", "password", "flashhunter", "123456789012"}:
        return False, "пароль слишком распространён"
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        return False, "пароль должен содержать минимум 3 класса символов"
    return True, "ok"


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------
def generate_totp_secret() -> str:
    """Случайный base32-секрет для TOTP."""
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str = "FlashHunter") -> str:
    """otpauth:// URI для QR-кода в UI."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """Проверка 6-значного кода TOTP (±1 шаг = ±30 сек дрейфа часов)."""
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=valid_window)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TokenPayload:
    """Разобранный payload access-токена."""

    sub: str
    sid: str
    exp: datetime
    must_change_password: bool = False
    role: str = "admin"


def create_access_token(
    username: str,
    *,
    session_id: str | None = None,
    must_change_password: bool = False,
    role: str = "admin",
    ttl_minutes: int | None = None,
) -> tuple[str, str]:
    """Создаёт JWT (HS256). Возвращает (token, session_id)."""
    settings = get_settings()
    sid = session_id or secrets.token_urlsafe(16)
    ttl = ttl_minutes or settings.jwt_ttl_minutes
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": username,
        "sid": sid,
        "role": role,
        "mcp": must_change_password,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
        "iss": "flashhunter",
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_alg)
    return token, sid


def decode_access_token(token: str) -> TokenPayload:
    """Декодирует и валидирует JWT. Бросает HTTPException(401) при любой проблеме."""
    settings = get_settings()
    try:
        raw = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_alg],
            issuer="flashhunter",
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    return TokenPayload(
        sub=raw["sub"],
        sid=raw["sid"],
        exp=datetime.fromtimestamp(raw["exp"], tz=UTC),
        must_change_password=bool(raw.get("mcp", False)),
        role=raw.get("role", "admin"),
    )


def token_fingerprint(token: str) -> str:
    """SHA-256 «отпечаток» токена — хранится в ``sessions.token_hash`` вместо самого токена."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time сравнение строк (для API-ключей, webhook-подписей)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Зависимости FastAPI
# ---------------------------------------------------------------------------
async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    """Достаёт пользователя из Bearer-токена или cookie-сессии; проверяет отзыв сессии."""
    token = creds.credentials if creds else request.cookies.get("fh_session")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated", {"WWW-Authenticate": "Bearer"})
    payload = decode_access_token(token)

    result = await db.execute(select(User).where(User.username == payload.sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")

    # Сессия должна существовать и не быть истёкшей (поддержка revoke-токенов)
    fingerprint = token_fingerprint(token)
    result = await db.execute(select(UserSession).where(UserSession.token_hash == fingerprint))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked")
    if session.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    return user


async def require_password_changed(user: User = Depends(get_current_user)) -> User:  # noqa: B008
    """Guard: до смены пароля доступны только /auth/me, /auth/change-password, /auth/totp/*."""
    if user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "password_change_required", "message": "Смените пароль admin перед продолжением"},
        )
    return user


async def require_totp_if_enabled(
    request: Request, user: User = Depends(require_password_changed)  # noqa: B008
) -> User:
    """Для критических действий: если у пользователя включён TOTP — требуется заголовок X-TOTP."""
    if user.totp_secret:
        code = request.headers.get("X-TOTP", "")
        if not verify_totp(user.totp_secret, code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing TOTP code")
    return user


# ---------------------------------------------------------------------------
# Rate limiting (in-memory; в cluster-режиме — Redis-токен-бакет)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Простой sliding-window лимитер для логина и тяжёлых ручек."""

    def __init__(self, max_calls: int, window_sec: float) -> None:
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """True — запрос разрешён; False — превышен лимит."""
        now = time.monotonic()
        bucket = [t for t in self._hits.get(key, []) if now - t < self.window_sec]
        if len(bucket) >= self.max_calls:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True

    def reset(self, key: str) -> None:
        """Сброс счётчика (например, после успешного логина)."""
        self._hits.pop(key, None)


login_limiter = RateLimiter(max_calls=10, window_sec=60)
heavy_limiter = RateLimiter(max_calls=30, window_sec=60)


def client_ip(request: Request) -> str:
    """Реальный IP клиента с учётом X-Forwarded-For (ingress/Traefik)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
