"""StrategyService — сердце orchestration-слоя.

Один асинхронный цикл делает всё, что в production распределено по сервисам:
    1) получает снимок блока (Rust ingestor/pool-indexer → здесь Mock/Live источник);
    2) обновляет кэш decimals и помечает сеть живой (детектор разрыва связи);
    3) параллельно прогоняет все включённые стратегии (asyncio.gather);
    4) дедуплицирует возможности и пишет их в БД (opportunities);
    5) лучшие пропускает через Executor (risk → simulation → bundle → relays → trades);
    6) публикует события в шину для WebSocket-клиентов UI.

Управление: start()/stop()/pause()/resume(), kill-switch — через риск-движок.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from flashhunter.config.settings import get_settings
from flashhunter.db.models import OpportunityStatus, ServiceHealth, StrategyConfig, Trade
from flashhunter.db.session import session_scope
from flashhunter.execution.executor import ExecutionResult, Executor
from flashhunter.chain.signer import KeeperWallet
from flashhunter.execution.chain_submit import OnChainSubmitter
from flashhunter.execution.relays import get_relay_client
from flashhunter.market.live import LiveMarketSource
from flashhunter.market.mock import MockMarketSource
from flashhunter.market.snapshot import CrossChainIntent, MarketSnapshot
from flashhunter.risk.engine import get_risk_engine
from flashhunter.strategies.arb import DexArbStrategy
from flashhunter.strategies.backrun import BackrunStrategy
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity
from flashhunter.strategies.intent import IntentStrategy
from flashhunter.strategies.jit import JitStrategy
from flashhunter.strategies.liq import LiquidationStrategy
from flashhunter.strategies.math.adapters import register_decimals
from flashhunter.strategies.stable_peg import StablePegStrategy
from flashhunter.strategies.triangular import TriangularStrategy

log = logging.getLogger(__name__)

STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "dex_arb": DexArbStrategy,
    "triangular": TriangularStrategy,
    "stable_peg": StablePegStrategy,
    "liquidation": LiquidationStrategy,
    "jit": JitStrategy,
    "backrun": BackrunStrategy,
    "intent": IntentStrategy,
}

BLOCK_INTERVAL_SEC = 1.0  # эмуляция 12-секундных блоков Ethereum, ускоренная для демо


@dataclass(slots=True)
class ServiceStats:
    """Счётчики сервиса для дашборда."""

    blocks_processed: int = 0
    opportunities_found: int = 0
    opportunities_executed: int = 0
    opportunities_rejected: int = 0
    reverted: int = 0
    total_latency_ms: float = 0.0
    last_pipeline_ms: float = 0.0        # стратегии + симуляция + отправка (без чтения сети)
    last_snapshot_ms: float = 0.0        # сколько заняло чтение реального состояния сети
    started_at: float = field(default_factory=time.time)

    @property
    def avg_latency_ms(self) -> float:
        """Средняя задержка обработки блока (метрика «block → bundle»)."""
        return (self.total_latency_ms / self.blocks_processed) if self.blocks_processed else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Снапшот статистики для API."""
        return {
            "blocks_processed": self.blocks_processed,
            "opportunities_found": self.opportunities_found,
            "opportunities_executed": self.opportunities_executed,
            "opportunities_rejected": self.opportunities_rejected,
            "reverted": self.reverted,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "uptime_sec": round(time.time() - self.started_at, 1),
        }


class StrategyService:
    """Оркестратор стратегий: блок → возможности → сделки → события."""

    def __init__(self, *, profile: str = "normal", max_executions_per_block: int = 2) -> None:
        self.settings = get_settings()
        self.risk = get_risk_engine()
        self.relays = get_relay_client()

        # Боевой модуль отправки. Он всегда создан (нужен для pre-flight и подписи),
        # но реально отправить транзакцию сможет только при наличии ключа кипера и
        # адреса контракта-исполнителя. Без них исполнитель откажет с явной причиной.
        self.wallet = KeeperWallet(self.settings.keeper_key(), chain_id=self.settings.live_chain_id)
        self.chain = OnChainSubmitter(
            self.settings.rpc_endpoints(self.settings.live_chain_id),
            self.wallet,
            chain_id=self.settings.live_chain_id,
            executor_contract=self.settings.flashloan_arbitrage_address,
            relays=self.settings.live_relays(),
            gas_ceiling_gwei=float(self.settings.exec_max_gas_gwei),
            priority_fee_gwei=float(self.settings.exec_priority_fee_gwei),
            receipt_timeout_sec=self.settings.exec_receipt_timeout_sec,
            confirmations=self.settings.exec_confirmations,
        )
        self.executor = Executor(self.risk, relays=self.relays, chain=self.chain)
        # Источник данных: реальная сеть (боевой режим) либо имитация для офлайн-отладки.
        # Решение принимается ровно по MOCK_MARKET_DATA — иначе «незаметно включить мок» невозможно.
        if self.settings.mock_market_data:
            log.warning(
                "MOCK_MARKET_DATA=true: рынок имитируется. Реальные сделки на таких данных "
                "невозможны — этот режим только для отладки и тестов."
            )
            self.market: MockMarketSource | LiveMarketSource = MockMarketSource(
                chain_id=self.settings.live_chain_id, profile=profile, seed=int(time.time()) % 10_000
            )
        else:
            self.market = LiveMarketSource(
                self.settings.rpc_endpoints(self.settings.live_chain_id),
                ws_url=self.settings.ws_endpoint(),
                subscribe_pending_txs=self.settings.live_ws_pending_txs,
                chain_id=self.settings.live_chain_id,
                aave_lookback_blocks=self.settings.live_aave_lookback_blocks,
                aave_max_users=self.settings.live_aave_max_users,
                aave_refresh_sec=self.settings.live_borrower_refresh_sec,
                watch_hf_threshold=self.settings.live_watch_hf_threshold,
                price_ttl_sec=self.settings.live_price_ttl_sec,
            )
            log.info(
                "Источник данных: реальная сеть %d (%d RPC-эндпоинтов)",
                self.settings.live_chain_id,
                len(self.settings.rpc_endpoints(self.settings.live_chain_id)),
            )
        self.strategies: list[BaseStrategy] = []
        self.stats = ServiceStats()
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._task: asyncio.Task[None] | None = None
        self._autosweep_task: asyncio.Task[None] | None = None
        self.running = False
        self.paused = False
        self.max_executions_per_block = max_executions_per_block
        self.last_snapshot: MarketSnapshot | None = None
        self.recent_opportunities: deque[dict[str, Any]] = deque(maxlen=200)
        # объекты (не сериализованные строки) — нужны для ручного исполнения из UI
        self.recent_opportunity_objects: deque[Any] = deque(maxlen=200)
        self._strategy_latency: dict[str, deque[float]] = {}
        self._last_persist = 0.0
        self._last_block_seen = 0

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Загружает конфигурацию стратегий и запускает цикл блоков."""
        if self.running:
            return
        if isinstance(self.market, LiveMarketSource):
            await self.market.start()
        await self.chain.start()
        await self._load_strategies()
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="strategy-loop")
        self._autosweep_task = asyncio.create_task(self._auto_sweep_loop(), name="auto-sweep")
        log.info("StrategyService started with %d strategies", len(self.strategies))

    async def stop(self) -> None:
        """Останавливает цикл и стратегии (graceful shutdown)."""
        self.running = False
        for task in (self._task, self._autosweep_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 — остановка не должна падать
                    pass
        for strategy in self.strategies:
            await strategy.on_stop()
        if isinstance(self.market, LiveMarketSource):
            await self.market.stop()
        await self.chain.close()
        log.info("StrategyService stopped: %s", self.stats.as_dict())

    def pause(self) -> None:
        """Пауза обработки (например, перед ручным вмешательством)."""
        self.paused = True

    def resume(self) -> None:
        """Возобновление обработки."""
        self.paused = False

    # ------------------------------------------------------------------ #
    # Загрузка стратегий
    # ------------------------------------------------------------------ #
    async def _load_strategies(self) -> None:
        """Читает strategies_config из БД и инстанцирует стратегии."""
        self.strategies = []
        async with session_scope() as session:
            rows = (await session.execute(select(StrategyConfig))).scalars().all()
        for row in rows:
            cls = STRATEGY_CLASSES.get(row.name)
            if cls is None:
                log.warning("unknown strategy in DB: %s", row.name)
                continue
            strategy = cls(params=row.params or {}, chains=row.chains or None)
            strategy.enabled = row.enabled
            if row.enabled:
                await strategy.on_start()
                self.strategies.append(strategy)
        if not self.strategies:
            log.warning("no enabled strategies; enabling dex_arb by default")
            default = DexArbStrategy()
            await default.on_start()
            self.strategies.append(default)

    # ------------------------------------------------------------------ #
    # Основной цикл
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        """Бесконечный цикл: блок → стратегии → исполнение → события."""
        while self.running:
            block_started = time.perf_counter()
            try:
                if not self.paused:
                    await self._process_block()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать от одной ошибки
                log.exception("block processing failed")
            elapsed = time.perf_counter() - block_started
            self.stats.total_latency_ms += elapsed * 1000
            # В риск-движок уходит латентность ПАЙПЛАЙНА (стратегии + симуляция + отправка),
            # без времени чтения состояния сети: в живой сети RPC может занять секунды,
            # и это не наша задержка принятия решения — иначе kill-switch сработает зря.
            pipeline_ms = self.stats.last_pipeline_ms if self.stats.last_pipeline_ms > 0 else elapsed * 1000
            self.risk.record_latency(pipeline_ms)
            interval = self.settings.block_interval_sec if hasattr(self.settings, "block_interval_sec") else BLOCK_INTERVAL_SEC
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _process_block(self) -> None:
        """Один блок: снимок, стратегии, дедуп, исполнение, запись."""
        snapshot_started = time.perf_counter()
        snapshot = self.market.next_snapshot()
        if inspect.isawaitable(snapshot):
            # Живой источник читает состояние сети: это настоящий сетевой вызов
            snapshot = await snapshot
        self.stats.last_snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        pipeline_started = time.perf_counter()

        # Один и тот же блок не обрабатываем дважды: в живой сети снимок может
        # быть сделан дважды подряд, пока не появился следующий блок.
        if snapshot.block_number and snapshot.block_number == self._last_block_seen:
            return
        self._last_block_seen = snapshot.block_number or self._last_block_seen
        self.last_snapshot = snapshot
        register_decimals(snapshot)  # важно: адаптеры Curve требуют decimals до расчётов
        self.risk.mark_chain_alive(snapshot.chain_id)
        self.stats.blocks_processed += 1

        ctx = BlockCtx(
            chain_id=snapshot.chain_id,
            block_number=snapshot.block_number,
            timestamp=snapshot.timestamp,
            base_fee_gwei=snapshot.base_fee_gwei,
            snapshot=snapshot,
        )

        found = await self._run_strategies(ctx)
        found = self._dedupe(found)
        found.sort(key=lambda o: o.net_profit_usd, reverse=True)
        # Латентность принятия решения: отбор + дедуп (без чтения сети и без БД)
        self.stats.last_pipeline_ms = (time.perf_counter() - pipeline_started) * 1000
        self.stats.opportunities_found += len(found)

        for opp in found[:25]:
            self.recent_opportunities.appendleft(opp.to_row())
            self.recent_opportunity_objects.appendleft(opp)
            await self._publish({"type": "opportunity", "data": opp.to_row()})

        if not found:
            return

        async with session_scope() as session:
            results: list[ExecutionResult] = []
            for opp in found[: self.max_executions_per_block]:
                await self.executor.persist_opportunity(session, opp, OpportunityStatus.detected)
                result = await self.executor.execute(opp, snapshot, session)
                results.append(result)
                await self._record_result(opp, result)
            for opp in found[self.max_executions_per_block :]:
                await self.executor.persist_opportunity(session, opp, OpportunityStatus.simulated)

        for result in results:
            payload = result.as_dict()
            await self._publish({"type": "execution", "data": payload})
            if result.accepted:
                self.stats.opportunities_executed += 1
            elif result.status == "reverted":
                self.stats.reverted += 1
            else:
                self.stats.opportunities_rejected += 1

    async def _run_strategies(self, ctx: BlockCtx) -> list[Any]:
        """Параллельный прогон стратегий с замером латентности каждой."""
        tasks = [strategy.on_block(ctx) for strategy in self.strategies if strategy.supports(ctx.chain_id)]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found: list[Any] = []
        for strategy, result in zip(
            [s for s in self.strategies if s.supports(ctx.chain_id)], results, strict=True
        ):
            if isinstance(result, BaseException):
                log.warning("strategy %s failed: %s", strategy.name, result)
                continue
            self._strategy_latency.setdefault(strategy.name, deque(maxlen=200)).append(
                time.perf_counter()
            )
            found.extend(result)
            for opp in result:
                opp.meta.setdefault("strategy_chain", ctx.chain_id)
        return found

    @staticmethod
    def _dedupe(opportunities: list[Any]) -> list[Any]:
        """Убирает дубликаты: одна и та же связка пулов у одной стратегии — оставляем лучшую."""
        best: dict[tuple[str, tuple[str, ...], str], Any] = {}
        for opp in opportunities:
            key = (opp.strategy, tuple(sorted(opp.pools)), opp.kind)
            current = best.get(key)
            if current is None or opp.net_profit_usd > current.net_profit_usd:
                best[key] = opp
        return list(best.values())

    # ------------------------------------------------------------------ #
    async def _record_result(self, opp: Any, result: ExecutionResult) -> None:
        """Публикует событие и обновляет метрики по итогам исполнения."""
        if result.accepted:
            log.info("TRADE %s %s net=$%.2f", opp.strategy, opp.uid, float(result.net_profit_usd))

    async def _auto_sweep_loop(self) -> None:
        """Автосивип прибыли на холодный кошелёк (ТЗ п.6/п.10) — раз в час."""
        interval = self.settings.risk_auto_sweep_interval_sec
        while self.running:
            await asyncio.sleep(interval)
            try:
                await self._sweep_profits()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("auto sweep failed")

    async def _sweep_profits(self) -> dict[str, Any]:
        """Формирует запись о сивипе (в dry-run — только событие, без on-chain перевода)."""
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Trade).where(Trade.status == "success").order_by(Trade.ts.desc()).limit(100)
                )
            ).scalars().all()
        total = sum(float(row.actual_profit_usd) for row in rows)
        event = {
            "type": "sweep",
            "data": {
                "total_usd": round(total, 2),
                "cold_wallet": self.settings.cold_wallet_address or "not_configured",
                "dry_run": self.risk.dry_run,
                "ts": datetime.now(UTC).isoformat(),
            },
        }
        await self._publish(event)
        return event["data"]

    # ------------------------------------------------------------------ #
    # Шина событий (WS fan-out)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Создаёт очередь подписчика (для WebSocket-клиента)."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Удаляет подписчика."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def _publish(self, event: dict[str, Any]) -> None:
        """Рассылает событие всем подписчикам (медленные клиенты отбрасываются)."""
        event.setdefault("ts", time.time())
        self.events.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("dropping event for slow subscriber")

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Последние события (для /logs и первичной отрисовки)."""
        return list(self.events)[-limit:]

    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        """Состояние сервиса для API (/dashboard, /strategies, /latency)."""
        snapshot = self.last_snapshot
        return {
            "running": self.running,
            "paused": self.paused,
            "dry_run": self.risk.dry_run,
            "kill_switch": self.risk.kill_switch,
            "kill_reason": self.risk.kill_reason,
            "stats": self.stats.as_dict(),
            "strategies": [
                {"name": s.name, "enabled": s.enabled, "chains": s.chains} for s in self.strategies
            ],
            "market": {
                "chain_id": snapshot.chain_id if snapshot else None,
                "block_number": snapshot.block_number if snapshot else None,
                "base_fee_gwei": snapshot.base_fee_gwei if snapshot else None,
                "native_price_usd": float(snapshot.native_price_usd) if snapshot else None,
                "pools": len(snapshot.pools) if snapshot else 0,
                "positions": len(snapshot.positions) if snapshot else 0,
                "pending_swaps": len(snapshot.pending_swaps) if snapshot else 0,
            },
            "relays": self.relays.stats_snapshot(),
            "risk": self.risk.snapshot_state(),
            "execution": {
                "mode": "dry_run" if self.risk.dry_run else "live",
                "market": "simulated" if self.settings.mock_market_data else "live_chain",
                "chain_id": self.settings.live_chain_id,
                **{k: v for k, v in self.chain.describe().items() if k not in {"chain_id"}},
            },
            "latency": {
                "snapshot_ms": round(self.stats.last_snapshot_ms, 1),
                "pipeline_ms": round(self.stats.last_pipeline_ms, 2),
                "avg_block_ms": round(self.stats.avg_latency_ms, 1),
            },
        }

    async def heartbeat(self) -> None:
        """Пишет пульс сервиса в service_health (для /dashboard и алертов)."""
        async with session_scope() as session:
            result = await session.execute(
                select(ServiceHealth).where(ServiceHealth.service == "strategy-service")
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = ServiceHealth(service="strategy-service")
                session.add(row)
            row.status = "ok" if (self.running and not self.paused and not self.risk.kill_switch) else "degraded"
            row.latency_ms = self.stats.avg_latency_ms
            row.last_seen = datetime.now(UTC).replace(tzinfo=None)
            row.details = self.stats.as_dict()


# ---------------------------------------------------------------------------
# Синглтон сервиса (используется API и CLI)
# ---------------------------------------------------------------------------
_service: StrategyService | None = None


def get_strategy_service() -> StrategyService:
    """Ленивый синглтон StrategyService."""
    global _service
    if _service is None:
        profile = "normal" if not get_settings().is_single_node else "normal"
        _service = StrategyService(profile=profile)
    return _service
