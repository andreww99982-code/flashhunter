"""Executor: путь сделки от найденной возможности до записи в БД.

Порядок: risk.check -> reserve exposure -> simulate -> bundle build -> relay submit
-> persist trade -> risk.record_outcome. Каждый шаг логируется метрикой латентности,
итоговая задержка «блок -> bundle отправлен» уходит в risk-engine (контроль p99).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from flashhunter.db.models import Opportunity as OpportunityRow
from flashhunter.db.models import OpportunityStatus, Trade, TradeStatus
from flashhunter.execution.bundle_builder import BundleBuilder, BundlePlan
from flashhunter.execution.chain_submit import ArbRoute, OnChainSubmitter, PreflightResult, SubmitOutcome
from flashhunter.execution.relays import RelayClient, RelayResponse
from flashhunter.execution.simulator import LocalSimulator, SimulationResult
from flashhunter.market.snapshot import MarketSnapshot
from flashhunter.risk.engine import RiskEngine, TradeOutcome
from flashhunter.strategies.base import Opportunity

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionResult:
    """Итог обработки одной возможности."""

    uid: str
    accepted: bool
    status: str
    net_profit_usd: Decimal
    bundle_hash: str = ""
    relay: str = ""
    reason: str = ""
    latency_ms: float = 0.0
    simulation: SimulationResult | None = None
    plan: BundlePlan | None = None
    trade_id: int | None = None
    relay_responses: list[RelayResponse] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для WS/логов."""
        return {
            "uid": self.uid,
            "accepted": self.accepted,
            "status": self.status,
            "net_profit_usd": float(self.net_profit_usd),
            "bundle_hash": self.bundle_hash,
            "relay": self.relay,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 2),
        }


class Executor:
    """Исполнитель сделок с полным контролем риска."""

    def __init__(
        self,
        risk: RiskEngine,
        *,
        simulator: LocalSimulator | None = None,
        builder: BundleBuilder | None = None,
        relays: RelayClient | None = None,
        chain: OnChainSubmitter | None = None,
    ) -> None:
        self.risk = risk
        self.simulator = simulator or LocalSimulator(slippage_tolerance_bps=risk.slippage_ceiling_bps)
        self.builder = builder or BundleBuilder()
        self.relays = relays or RelayClient()
        # Боевой путь отправки: реальные транзакции в сеть. Может быть None — тогда
        # в dry-run всё работает как раньше, а live-режим честно отказывает.
        self.chain = chain

    # ------------------------------------------------------------------ #
    async def execute(
        self, opp: Opportunity, snapshot: MarketSnapshot, session: AsyncSession
    ) -> ExecutionResult:
        """Полный цикл исполнения одной возможности."""
        started = time.perf_counter()
        size_usd = opp.size_usd
        net = opp.net_profit_usd

        # 1. Риск-контроль ------------------------------------------------
        decision = self.risk.check(
            chain_id=opp.chain_id,
            token_in=opp.token_in,
            size_usd=size_usd,
            net_profit_usd=net,
            base_fee_gwei=snapshot.base_fee_gwei,
            slippage_bps=self.risk.slippage_ceiling_bps,
            protocol=str(opp.meta.get("protocol", "")),
        )
        if not decision.allow:
            latency = (time.perf_counter() - started) * 1000
            log.info("opportunity %s rejected by risk: %s", opp.uid, decision.reason)
            return ExecutionResult(opp.uid, False, "rejected", net, reason=decision.reason,
                                   latency_ms=latency)

        self.risk.reserve_exposure(opp.chain_id, opp.token_in, size_usd)

        # 2. Симуляция ----------------------------------------------------
        simulation = await self.simulator.simulate(opp, snapshot)
        if not simulation.success:
            latency = (time.perf_counter() - started) * 1000
            await self._persist_trade(session, opp, TradeStatus.reverted, Decimal("0"),
                                      revert_reason=simulation.revert_reason, latency_ms=latency)
            self.risk.record_outcome(
                TradeOutcome(time.time(), opp.chain_id, Decimal("0"), "reverted", latency,
                             size_usd=size_usd, token=opp.token_in)
            )
            return ExecutionResult(opp.uid, False, "reverted", Decimal("0"),
                                   reason=simulation.revert_reason, latency_ms=latency,
                                   simulation=simulation)

        # 3. Сборка bundle ------------------------------------------------
        plan = self.builder.build(opp, snapshot, gas_used=simulation.gas_used,
                                  sender=str(opp.meta.get("sender", "")))

        # 4. Боевой путь: pre-flight -> подпись -> отправка -> подтверждение ---
        # В dry-run транзакция не уходит в сеть: считаем экономику и показываем план.
        if not self.risk.dry_run:
            return await self._execute_live(
                opp=opp,
                snapshot=snapshot,
                session=session,
                plan=plan,
                simulation=simulation,
                started=started,
                size_usd=size_usd,
                net=net,
            )

        # 4b. Dry-run: смоделированный ответ релеев -----------------------
        responses: list[RelayResponse] = []
        accepted = [r for r in responses if r.accepted]
        relay = accepted[0].relay if accepted else ("dry_run" if self.risk.dry_run else "")

        status = TradeStatus.success if (self.risk.dry_run or accepted) else TradeStatus.dropped
        reason = "dry_run" if self.risk.dry_run else ("" if accepted else "no_relay_accepted")
        profit = net if self.risk.dry_run else (net if accepted else Decimal("0"))

        # 5. Персистентность ---------------------------------------------
        trade_id = await self._persist_trade(
            session, opp, status, profit, bundle_hash=plan.bundle_hash, relay=relay,
            revert_reason=reason, latency_ms=(time.perf_counter() - started) * 1000,
            gas_used=simulation.gas_used, extra={"simulation": simulation.as_dict()},
        )

        latency_ms = (time.perf_counter() - started) * 1000
        self.risk.record_outcome(
            TradeOutcome(time.time(), opp.chain_id, profit, status.value, latency_ms,
                         size_usd=size_usd, token=opp.token_in)
        )
        self.risk.record_latency(latency_ms)

        log.info(
            "executed %s via %s: status=%s profit=$%.2f latency=%.1fms",
            opp.uid, relay or "n/a", status.value, float(profit), latency_ms,
        )
        return ExecutionResult(
            uid=opp.uid,
            accepted=status == TradeStatus.success,
            status=status.value,
            net_profit_usd=profit,
            bundle_hash=plan.bundle_hash,
            relay=relay,
            reason=reason,
            latency_ms=latency_ms,
            simulation=simulation,
            plan=plan,
            trade_id=trade_id,
            relay_responses=responses,
        )

    # ------------------------------------------------------------------ #
    # Боевое исполнение: настоящая транзакция в сети
    # ------------------------------------------------------------------ #
    async def _execute_live(
        self,
        *,
        opp: Opportunity,
        snapshot: MarketSnapshot,
        session: AsyncSession,
        plan: BundlePlan,
        simulation: SimulationResult,
        started: float,
        size_usd: Decimal,
        net: Decimal,
    ) -> ExecutionResult:
        """Отправляет реальную сделку: pre-flight, подпись, релеи, подтверждение.

        Каждый шаг делается против настоящей сети. Если ключа нет или контракт не
        задан, сделка помечается ``rejected`` с точной причиной — выдавать успех
        без транзакции в сети недопустимо.
        """
        helper = self.chain
        if helper is None or not helper.executor_contract:
            latency = (time.perf_counter() - started) * 1000
            reason = "боевой режим без контракта-исполнителя: укажите FLASHLOAN_ARBITRAGE_ADDRESS"
            await self._persist_trade(session, opp, TradeStatus.dropped, Decimal("0"),
                                      revert_reason=reason, latency_ms=latency)
            return ExecutionResult(opp.uid, False, "dropped", Decimal("0"), reason=reason,
                                   latency_ms=latency, simulation=simulation, plan=plan)

        if not helper.wallet.can_sign:
            latency = (time.perf_counter() - started) * 1000
            reason = ("боевой режим без ключа кипера: укажите KEEPER_PRIVATE_KEY или KEEPER_KEY_FILE "
                      "(без ключа отправка невозможна)")
            await self._persist_trade(session, opp, TradeStatus.dropped, Decimal("0"),
                                      revert_reason=reason, latency_ms=latency)
            return ExecutionResult(opp.uid, False, "rejected", Decimal("0"), reason=reason,
                                   latency_ms=latency, simulation=simulation, plan=plan)

        # --- Маршрут для контракта: роутеры + пути + защита от проскальзывания ---
        route = self._build_route(opp, snapshot)
        if route is None:
            latency = (time.perf_counter() - started) * 1000
            reason = "не удалось собрать маршрут для контракта (нет роутера или пути свопа)"
            await self._persist_trade(session, opp, TradeStatus.dropped, Decimal("0"),
                                      revert_reason=reason, latency_ms=latency)
            return ExecutionResult(opp.uid, False, "rejected", Decimal("0"), reason=reason,
                                   latency_ms=latency, simulation=simulation, plan=plan)

        # --- Pre-flight: тот же вызов, но без списания газа ------------------
        preflight = await helper.preflight(opp.token_in, opp.size_in, route)
        if not preflight.ok:
            latency = (time.perf_counter() - started) * 1000
            await self._persist_trade(session, opp, TradeStatus.reverted, Decimal("0"),
                                      revert_reason=preflight.reason, latency_ms=latency,
                                      extra={"preflight": preflight.as_dict()})
            self.risk.record_outcome(
                TradeOutcome(time.time(), opp.chain_id, Decimal("0"), "reverted", latency,
                             size_usd=size_usd, token=opp.token_in)
            )
            return ExecutionResult(opp.uid, False, "reverted", Decimal("0"), reason=preflight.reason,
                                   latency_ms=latency, simulation=simulation, plan=plan)

        # --- Экономика газа по реальной оценке сети --------------------------
        gas_limit = max(int(preflight.gas_estimate * 1.15), int(preflight.gas_estimate) + 5_000)
        gas_cost_usd = self._gas_cost_usd(gas_limit, snapshot)
        if gas_cost_usd >= net:
            latency = (time.perf_counter() - started) * 1000
            reason = (f"газ ${gas_cost_usd:.2f} съедает прибыль ${float(net):.2f}: "
                      f"сделка не отправляется (реальная оценка газа {preflight.gas_estimate})")
            await self._persist_trade(session, opp, TradeStatus.dropped, Decimal("0"),
                                      revert_reason=reason, latency_ms=latency,
                                      extra={"preflight": preflight.as_dict()})
            return ExecutionResult(opp.uid, False, "rejected", Decimal("0"), reason=reason,
                                   latency_ms=latency, simulation=simulation, plan=plan)

        # --- Отправка --------------------------------------------------------
        outcome = await helper.submit(
            asset=opp.token_in,
            amount=opp.size_in,
            route=route,
            gas_limit=gas_limit,
            block_number=opp.block_number,
        )
        latency = (time.perf_counter() - started) * 1000

        status = {
            "confirmed": TradeStatus.success,
            "reverted": TradeStatus.reverted,
            "submitted": TradeStatus.pending,
            "dropped": TradeStatus.dropped,
            "no_signer": TradeStatus.dropped,
            "rejected": TradeStatus.dropped,
        }.get(outcome.status, TradeStatus.dropped)

        # Прибыль берём из события контракта: это факт из блокчейна, а не наша модель
        profit_usd = Decimal("0")
        if outcome.status == "confirmed" and outcome.profit_wei:
            price = snapshot.token(opp.token_in).price_usd
            profit_usd = (Decimal(outcome.profit_wei) / Decimal(10**snapshot.token(opp.token_in).decimals)) * price
        elif outcome.status == "confirmed" and not outcome.profit_wei:
            profit_usd = net  # событие не найдено: оставляем модельную оценку, помечая это в extra

        trade_id = await self._persist_trade(
            session,
            opp,
            status,
            profit_usd,
            bundle_hash=plan.bundle_hash,
            relay=outcome.relay,
            revert_reason=outcome.reason,
            latency_ms=latency,
            gas_used=outcome.gas_used or gas_limit,
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            extra={
                "simulation": simulation.as_dict(),
                "preflight": preflight.as_dict(),
                "onchain": outcome.as_dict(),
                "profit_source": "event" if outcome.profit_wei else "estimate",
            },
        )

        self.risk.record_outcome(
            TradeOutcome(time.time(), opp.chain_id, profit_usd, status.value, latency,
                         size_usd=size_usd, token=opp.token_in)
        )
        self.risk.record_latency(latency)

        log.info(
            "боевая сделка %s: статус=%s хэш=%s релей=%s газ=%d прибыль=$%.2f",
            opp.uid, outcome.status, outcome.tx_hash or "—", outcome.relay or "—",
            outcome.gas_used, float(profit_usd),
        )
        return ExecutionResult(
            uid=opp.uid,
            accepted=status == TradeStatus.success,
            status=status.value,
            net_profit_usd=profit_usd,
            bundle_hash=plan.bundle_hash,
            relay=outcome.relay,
            reason=outcome.reason,
            latency_ms=latency,
            simulation=simulation,
            plan=plan,
            trade_id=trade_id,
        )

    def _build_route(self, opp: Opportunity, snapshot: MarketSnapshot) -> ArbRoute | None:
        """Переводит возможность в маршрут контракта: роутеры, пути, minProfit, deadline."""
        if self.chain is None:
            return None
        meta = opp.meta or {}
        buy_dex = str(meta.get("dex_buy", "")) or str(meta.get("buy_dex", ""))
        sell_dex = str(meta.get("dex_sell", "")) or str(meta.get("sell_dex", ""))
        buy_router = str(meta.get("buy_router", "")) or self.chain.router_for(buy_dex)
        sell_router = str(meta.get("sell_router", "")) or self.chain.router_for(sell_dex)
        if not buy_router or not sell_router:
            return None

        # Пути свопов: либо заданы стратегией, либо строятся по токенам сделки
        buy_path = meta.get("buy_path") or [opp.token_in, opp.token_out]
        sell_path = meta.get("sell_path") or [opp.token_out, opp.token_in]
        if len(buy_path) < 2 or len(sell_path) < 2:
            return None

        # minProfit: сколько минимум должен вернуть контракт. Берём прибыль с запасом
        # на проскальзывание — контракт сам ревертнёт, если рынок ушёл против нас.
        tolerance_bps = Decimal(self.risk.slippage_ceiling_bps)
        expected = Decimal(opp.profit_value) * (Decimal(1) - tolerance_bps / Decimal(10_000))
        if meta.get("min_profit_units"):
            expected = Decimal(str(meta["min_profit_units"]))
        min_profit = max(int(expected), 0)

        deadline = int(snapshot.timestamp) + 45  # 3 блока: позже включать транзакцию нельзя
        return ArbRoute(
            buy_router=buy_router,
            sell_router=sell_router,
            buy_path=[str(t) for t in buy_path],
            sell_path=[str(t) for t in sell_path],
            min_profit=min_profit,
            deadline=deadline,
        )

    def _gas_cost_usd(self, gas_units: int, snapshot: MarketSnapshot) -> Decimal:
        """Стоимость газа в USD по текущей цене base fee и курсу сети."""
        base_fee = Decimal(str(snapshot.base_fee_gwei)) + Decimal(str(self.settings.exec_priority_fee_gwei))
        native = snapshot.native_price_usd if snapshot.native_price_usd else Decimal("0")
        return Decimal(gas_units) * base_fee * Decimal("1e-9") * native

    # ------------------------------------------------------------------ #
    async def _persist_trade(
        self,
        session: AsyncSession,
        opp: Opportunity,
        status: TradeStatus,
        profit: Decimal,
        *,
        bundle_hash: str = "",
        relay: str = "",
        revert_reason: str = "",
        latency_ms: float = 0.0,
        gas_used: int = 0,
        extra: dict[str, Any] | None = None,
        tx_hash: str = "",
        block_number: int = 0,
    ) -> int:
        """Пишет сделку в БД (trades) и возвращает её id."""
        trade = Trade(
            opportunity_id=int(opp.meta.get("db_id", 0)) or None,
            chain_id=opp.chain_id,
            strategy=opp.strategy,
            # Хэш реальной транзакции в боевом режиме; в dry-run его нет и быть не может
            tx_hash=(tx_hash or None) if not self.risk.dry_run else None,
            bundle_hash=bundle_hash,
            relay=relay or None,
            block_number=block_number or opp.block_number,
            gas_used=gas_used,
            gas_price_gwei=float(self.builder.settings.exec_priority_fee_gwei),
            priority_fee_gwei=float(self.builder.settings.exec_priority_fee_gwei),
            actual_profit_usd=profit,
            status=status,
            revert_reason=(revert_reason or None) and revert_reason[:250],
            latency_ms=int(latency_ms),
            details=json.dumps(extra, default=str, ensure_ascii=False) if extra else None,
        )
        session.add(trade)
        await session.flush()
        return trade.id

    # ------------------------------------------------------------------ #
    async def persist_opportunity(
        self, session: AsyncSession, opp: Opportunity, status: OpportunityStatus
    ) -> int:
        """Пишет найденную возможность в БД (opportunities) и возвращает её id."""
        row = OpportunityRow(
            chain_id=opp.chain_id,
            strategy=opp.strategy,
            token_in=opp.token_in,
            token_out=opp.token_out,
            dex_buy=opp.dex_buy,
            dex_sell=opp.dex_sell,
            path={
                "pools": opp.pools,
                "tokens": opp.tokens,
                "kind": opp.kind,
                "route": opp.meta.get("route", ""),
                "uid": opp.uid,
            },
            size_usd=opp.size_usd,
            gross_profit_usd=opp.profit_usd,
            gas_usd=opp.gas_usd,
            flash_fee_usd=opp.flash_fee_usd,
            net_profit_usd=opp.net_profit_usd,
            roi_pct=opp.roi_pct,
            confidence=opp.confidence,
            status=status,
            block_number=opp.block_number,
        )
        session.add(row)
        await session.flush()
        opp.meta["db_id"] = str(row.id)
        return row.id
