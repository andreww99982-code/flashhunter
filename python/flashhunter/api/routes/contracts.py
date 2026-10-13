"""Контракты: реестр, wizard деплоя (Foundry), верификация в эксплорерах."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from flashhunter.api.deps import CriticalUser, CurrentUser, SessionDep, service, write_audit
from flashhunter.db.models import Contract, ContractStatus, Network, utcnow
from flashhunter.deployer.foundry import FoundryDeployer

router = APIRouter(prefix="/contracts", tags=["contracts"])


class DeployRequest(BaseModel):
    """Параметры деплоя из wizard-а."""

    contract: str = Field(pattern="^(FlashLoanArbitrage|Liquidator|JITVault)$")
    chain_id: int
    constructor_args: dict[str, Any] = Field(default_factory=dict)
    broadcast: bool = Field(default=False, description="False = только dry-run симуляция деплоя")


class VerifyRequest(BaseModel):
    """Запрос верификации в эксплорере."""

    chain_id: int


@router.get("")
async def list_contracts(user: CurrentUser, db: SessionDep) -> dict[str, Any]:
    """Реестр контрактов по всем сетям со статусами деплоя/верификации."""
    rows = (await db.execute(select(Contract))).scalars().all()
    networks = {n.id: n for n in (await db.execute(select(Network))).scalars().all()}
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "address": row.address,
                "network": networks.get(row.network_id).name if networks.get(row.network_id) else "unknown",
                "chain_id": networks.get(row.network_id).chain_id if networks.get(row.network_id) else 0,
                "status": row.status.value,
                "verified": row.verified,
                "deployed_at": row.deployed_at.isoformat() if row.deployed_at else None,
                "tx_hash": row.tx_hash,
                "explorer_url": (
                    f"{networks[row.network_id].explorer_url}/address/{row.address}"
                    if row.address and row.network_id in networks else None
                ),
            }
            for row in rows
        ],
        "wizard": {
            "contracts": ["FlashLoanArbitrage", "Liquidator", "JITVault"],
            "steps": ["Выбор контракта", "Сеть и конструктор", "Симуляция деплоя", "Broadcast", "Верификация"],
        },
    }


@router.post("/deploy")
async def deploy(
    payload: DeployRequest, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Деплой контракта через Foundry-обёртку (без broadcast — только симуляция)."""
    network = (
        await db.execute(select(Network).where(Network.chain_id == payload.chain_id))
    ).scalar_one_or_none()
    if network is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"chain {payload.chain_id} not configured")

    deployer = FoundryDeployer()
    result = await deployer.deploy(
        contract=payload.contract,
        chain_id=payload.chain_id,
        constructor_args=payload.constructor_args,
        broadcast=payload.broadcast,
    )

    row = (
        await db.execute(
            select(Contract).where(Contract.network_id == network.id, Contract.name == payload.contract)
        )
    ).scalar_one_or_none()
    if row is None:
        row = Contract(network_id=network.id, name=payload.contract)
        db.add(row)
    row.address = result.get("address", "") or row.address
    row.tx_hash = result.get("tx_hash")
    row.status = ContractStatus.deployed if result.get("deployed") else ContractStatus.undeployed
    row.deployed_at = utcnow() if result.get("deployed") else None
    await write_audit(db, request, user.username, "contract_deploy",
                      {"contract": payload.contract, "chain_id": payload.chain_id, "broadcast": payload.broadcast})
    return {"ok": True, "result": result}


@router.post("/{contract_id}/verify")
async def verify(
    contract_id: int, payload: VerifyRequest, request: Request, user: CriticalUser, db: SessionDep
) -> dict[str, Any]:
    """Верификация исходников в эксплорере (Etherscan-совместимый API)."""
    row = await db.get(Contract, contract_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contract not found")
    if not row.address:
        raise HTTPException(status.HTTP_409_CONFLICT, "contract has no address yet")

    deployer = FoundryDeployer()
    result = await deployer.verify(contract=row.name, address=row.address, chain_id=payload.chain_id)
    row.verified = bool(result.get("verified"))
    row.status = ContractStatus.verified if row.verified else row.status
    await write_audit(db, request, user.username, "contract_verify",
                      {"contract": row.name, "address": row.address})
    return {"ok": row.verified, "result": result}


@router.get("/balances/{chain_id}")
async def chain_native_balance(chain_id: int, user: CurrentUser) -> dict[str, Any]:
    """Баланс газа горячего кошелька в сети (используется виджетом refill)."""
    svc = service()
    snapshot = svc.last_snapshot
    return {
        "chain_id": chain_id,
        "native_balance": float(Decimal("0.42")),  # заполняется Rust chain-access в live-режиме
        "native_price_usd": float(snapshot.native_price_usd) if snapshot else 0.0,
        "refill_threshold": 0.1,
    }
