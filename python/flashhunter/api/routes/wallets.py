"""Кошельки: реестр, добавление (hot/cold), refill, план автосивипа прибыли."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from eth_account import Account

from flashhunter.api.deps import CriticalUser, CurrentUser, SessionDep, service, write_audit
from flashhunter.config.secrets import encrypt_secret, mask_secret
from flashhunter.db.models import Setting, Trade, TradeStatus, Wallet, WalletKind

router = APIRouter(prefix="/wallets", tags=["wallets"])


class WalletCreate(BaseModel):
    """Добавление кошелька: hot (для подписи) или cold (для хранения)."""

    name: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$")
    private_key: str | None = Field(default=None, description="Хранится только зашифрованным")
    is_hot: bool = True
    chain_scope: list[int] = Field(default_factory=lambda: [1], min_length=1)

    @field_validator("chain_scope")
    @classmethod
    def valid_chain_scope(cls, value: list[int]) -> list[int]:
        if any(chain_id < 1 for chain_id in value):
            raise ValueError("chain IDs must be positive")
        return list(dict.fromkeys(value))


class RefillRequest(BaseModel):
    """Пополнение газа/баланса кошелька (план перевода)."""

    chain_id: int
    amount_native: float = Field(gt=0)


class SetKeyRequest(BaseModel):
    """Приватный ключ приходит телом запроса (не query-параметром, чтобы не попадать в логи URL)."""

    private_key: str = Field(min_length=64, max_length=66)


def _validate_wallet_key(address: str, private_key: str) -> None:
    try:
        derived_address = Account.from_key(private_key).address
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid private key") from exc
    if derived_address.lower() != address.lower():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "wallet address does not match its private key")


@router.get("")
async def list_wallets(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Список кошельков с балансами и областью применения (сети)."""
    rows = (await db.execute(select(Wallet))).scalars().all()
    selected_trading = await db.get(Setting, "trading_wallet")
    trading_wallet_id = (selected_trading.value or {}).get("wallet_id") if selected_trading else None
    svc = service()
    snapshot = svc.last_snapshot
    return {
        "items": [
            {
                "id": w.id,
                "name": w.name,
                "address": w.address,
                "is_hot": w.is_hot,
                "kind": w.kind.value,
                "chains": w.chain_scope,
                "balance_usd": float(w.balance_usd),
                "native_balance": float(w.native_balance),
                "has_key": bool(w.encrypted_key),
                "is_trading": w.id == trading_wallet_id,
                "hsm_ref": w.hsm_ref,
                "created_at": w.created_at.isoformat(),
            }
            for w in rows
        ],
        "native_price_usd": float(snapshot.native_price_usd) if snapshot else 0.0,
        "sweep": {
            "interval_sec": svc.settings.risk_auto_sweep_interval_sec,
            "cold_wallet": svc.settings.cold_wallet_address or None,
            "min_amount_usd": float(svc.settings.sweep_min_amount_usd),
        },
    }


@router.post("")
async def create_wallet(
    payload: WalletCreate, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Добавляет кошелёк. Приватный ключ шифруется (Vault/KMS в prod, локально — AES-подобно)."""
    existing = (await db.execute(select(Wallet).where(Wallet.address == payload.address))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "wallet with this address already exists")
    if payload.private_key:
        _validate_wallet_key(payload.address, payload.private_key)
    wallet = Wallet(
        name=payload.name,
        address=payload.address,
        encrypted_key=encrypt_secret(payload.private_key) if payload.private_key else None,
        is_hot=payload.is_hot,
        kind=WalletKind.hot if payload.is_hot else WalletKind.cold,
        chain_scope=payload.chain_scope,
    )
    db.add(wallet)
    await db.flush()
    await write_audit(db, request, user.username, "wallet_add",
                      {"address": payload.address, "is_hot": payload.is_hot})
    return {"ok": True, "id": wallet.id, "address": wallet.address}


@router.post("/{wallet_id}/key")
async def set_key(
    wallet_id: int, payload: SetKeyRequest, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Устанавливает/заменяет приватный ключ (ответ маскируется)."""
    wallet = await db.get(Wallet, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "wallet not found")
    _validate_wallet_key(wallet.address, payload.private_key)
    wallet.encrypted_key = encrypt_secret(payload.private_key)
    await write_audit(db, request, user.username, "wallet_set_key", {"address": wallet.address})
    return {"ok": True, "masked": mask_secret(payload.private_key)}


@router.post("/{wallet_id}/refill")
async def refill(
    wallet_id: int, payload: RefillRequest, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Формирует перевод газа с холодного кошелька на горячий (в dry-run — только план)."""
    wallet = await db.get(Wallet, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "wallet not found")
    svc = service()
    plan = {
        "to": wallet.address,
        "chain_id": payload.chain_id,
        "amount_native": payload.amount_native,
        "dry_run": svc.risk.dry_run,
        "executed": False,
    }
    await write_audit(db, request, user.username, "wallet_refill", plan)
    return {"ok": True, "plan": plan}


@router.get("/sweep/plan")
async def sweep_plan(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """План автосивипа: сколько прибыли уходит на холодный кошелёк."""
    svc = service()
    rows = (
        await db.execute(select(Trade).where(Trade.status == TradeStatus.success))
    ).scalars().all()
    total = sum(float(t.actual_profit_usd) for t in rows)
    threshold = float(svc.settings.sweep_min_amount_usd)
    return {
        "accumulated_usd": round(total, 2),
        "threshold_usd": threshold,
        "will_sweep": total >= threshold,
        "cold_wallet": svc.settings.cold_wallet_address or "not_configured",
        "interval_sec": svc.settings.risk_auto_sweep_interval_sec,
        "dry_run": svc.risk.dry_run,
    }


@router.post("/sweep/run")
async def sweep_now(request: Request, user: CriticalUser, db: SessionDep) -> dict[str, Any]:
    """Немедленный сивип прибыли (в dry-run — только событие в журнале)."""
    svc = service()
    result = await svc._sweep_profits()
    await write_audit(db, request, user.username, "sweep_run", result)
    return {"ok": True, "result": result}
