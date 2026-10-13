"""Рутины аутентификации: вход, смена пароля, TOTP, выход."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from flashhunter.api.auth import (
    RateLimiter,
    client_ip,
    create_access_token,
    decode_access_token,
    generate_totp_secret,
    hash_password,
    login_limiter,
    password_strength_ok,
    token_fingerprint,
    totp_uri,
    verify_password,
    verify_totp,
)
from flashhunter.api.deps import SessionDep, UserDep, write_audit
from flashhunter.config.settings import get_settings
from flashhunter.db.models import Session as UserSession
from flashhunter.db.models import User, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Запрос на вход."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=8)


class LoginResponse(BaseModel):
    """Ответ на успешный вход."""

    token: str
    username: str
    must_change_password: bool
    totp_enabled: bool
    expires_in_minutes: int


class ChangePasswordRequest(BaseModel):
    """Смена пароля (обязательна для admin/admin)."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class TotpVerifyRequest(BaseModel):
    """Подтверждение TOTP-кода при включении 2FA."""

    code: str = Field(min_length=6, max_length=8)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response, db: SessionDep) -> LoginResponse:
    """Вход по логину/паролю (+TOTP, если включён). Rate limit: 10 попыток/минуту с IP."""
    settings = get_settings()
    ip = client_ip(request)
    if not login_limiter.check(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts, retry in a minute")

    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        log.warning("failed login for %s from %s", payload.username, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if user.totp_secret:
        if not payload.totp or not verify_totp(user.totp_secret, payload.totp):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing TOTP code")

    token, sid = create_access_token(
        user.username,
        must_change_password=user.must_change_password,
        role=user.role,
    )
    from datetime import timedelta

    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_fingerprint(token),
            expires_at=utcnow() + timedelta(minutes=settings.jwt_ttl_minutes),
            ip=ip,
            user_agent=request.headers.get("user-agent", "")[:250],
        )
    )
    user.last_login = utcnow()
    login_limiter.reset(ip)
    await write_audit(db, request, user.username, "login", {"success": True})

    response.set_cookie(
        "fh_session",
        token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.jwt_ttl_minutes * 60,
    )
    return LoginResponse(
        token=token,
        username=user.username,
        must_change_password=user.must_change_password,
        totp_enabled=bool(user.totp_secret),
        expires_in_minutes=settings.jwt_ttl_minutes,
    )


@router.post("/logout")
async def logout(request: Request, db: SessionDep) -> dict[str, bool]:
    """Отзывает текущую сессию (удаляет запись sessions) и сбрасывает cookie."""
    token = request.cookies.get("fh_session") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    if token:
        result = await db.execute(
            select(UserSession).where(UserSession.token_hash == token_fingerprint(token))
        )
        session_row = result.scalar_one_or_none()
        if session_row is not None:
            await db.delete(session_row)
    return {"ok": True}


@router.get("/me")
async def me(user: UserDep) -> dict[str, object]:
    """Текущий пользователь и его права."""
    return {
        "username": user.username,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "totp_enabled": bool(user.totp_secret),
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest, request: Request, user: UserDep, db: SessionDep
) -> dict[str, object]:
    """Смена пароля. Снимает флаг must_change_password и отзывает все прочие сессии."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    ok, reason = password_strength_ok(payload.new_password)
    if not ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    # при смене пароля все прежние сессии инвалидируются
    result = await db.execute(select(UserSession).where(UserSession.user_id == user.id))
    for session_row in result.scalars().all():
        await db.delete(session_row)
    await write_audit(db, request, user.username, "change_password", {})

    token, _sid = create_access_token(user.username, must_change_password=False, role=user.role)
    from datetime import timedelta

    settings = get_settings()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_fingerprint(token),
            expires_at=utcnow() + timedelta(minutes=settings.jwt_ttl_minutes),
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:250],
        )
    )
    return {"ok": True, "token": token}


def _qr_data_uri(payload: str) -> str:
    """Рендерит QR в data-URI (SVG), чтобы UI не тянул внешние картинки и не светил секрет наружу."""
    try:
        import base64
        from io import BytesIO

        import qrcode
        import qrcode.image.svg

        image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = BytesIO()
        image.save(buffer)
        return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — без библиотеки UI просто покажет секрет текстом
        log.warning("qrcode библиотека недоступна — отдаём только otpauth-URI")
        return ""


@router.post("/totp/setup")
async def totp_setup(user: UserDep) -> dict[str, str]:
    """Генерирует секрет, otpauth-URI и QR-код для подключения 2FA (пока не активирует)."""
    secret = generate_totp_secret()
    uri = totp_uri(secret, user.username)
    return {"secret": secret, "uri": uri, "qr": _qr_data_uri(uri)}


@router.post("/totp/enable")
async def totp_enable(payload: TotpVerifyRequest, request: Request, user: UserDep, db: SessionDep) -> dict[str, object]:
    """Активирует TOTP после проверки кода (секрет уже должен быть сохранён админом)."""
    if not user.totp_secret:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "сначала вызовите /auth/totp/setup и сохраните секрет",
        )
    if not verify_totp(user.totp_secret, payload.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid TOTP code")
    await write_audit(db, request, user.username, "totp_enabled", {})
    return {"ok": True, "totp_enabled": True}


@router.delete("/sessions")
async def revoke_sessions(request: Request, user: UserDep, db: SessionDep) -> dict[str, int]:
    """Отзывает все сессии пользователя (кроме текущей при желании оператора)."""
    result = await db.execute(select(UserSession).where(UserSession.user_id == user.id))
    sessions = list(result.scalars().all())
    for session_row in sessions:
        await db.delete(session_row)
    await write_audit(db, request, user.username, "revoke_sessions", {"count": len(sessions)})
    return {"revoked": len(sessions)}


__all__ = ["router", "RateLimiter", "decode_access_token"]
