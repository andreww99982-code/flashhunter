"""Risk Engine: kill-switch, лимиты экспозиции, потолки газа и слиппеджа, dry-run.

Принципы (ТЗ п.6):
    * любая сделка проходит ``check()`` ДО отправки; отказ = сделка не уходит;
    * окна потерь/ревертов скользящие (deque с таймстампами), память O(окно);
    * kill-switch взводится автоматически и требует ручного снятия из UI;
    * линейка проверок возвращает полный список причин отказа — это важно для UI.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from flashhunter.config.settings import get_settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskDecision:
    """Результат проверки сделки риск-движком."""

    allow: bool
    reason: str = ""
    checks: dict[str, Any] = field(default_factory=dict)

    def deny(self, reason: str, **checks: Any) -> RiskDecision:
        """Помечает решение как запрещающее."""
        self.allow = False
        self.reason = reason
        self.checks.update(checks)
        return self


@dataclass(slots=True)
class TradeOutcome:
    """Результат исполненной сделки для скользящих окон риска."""

    ts: float
    chain_id: int
    profit_usd: Decimal
    status: str  # success | reverted | dropped
    latency_ms: float
    size_usd: Decimal = Decimal("0")  # зарезервированный объём — освобождается по факту сделки
    token: str = ""


class RiskEngine:
    """Состояние риска: окна убытков/ревертов, экспозиция, kill-switch."""

    def __init__(self) -> None:
        settings = get_settings()
        self.daily_loss_limit = settings.risk_daily_loss_limit_usd
        self.hourly_loss_limit = settings.risk_hourly_loss_limit_usd
        self.revert_rate_limit = settings.risk_revert_rate_limit_pct
        self.latency_p99_limit_ms = settings.risk_latency_p99_limit_ms
        self.connection_loss_sec = settings.risk_connection_loss_sec
        self.max_exposure_chain = settings.risk_max_exposure_per_chain_usd
        self.max_exposure_token = settings.risk_max_exposure_per_token_usd
        self.gas_ceiling_gwei = settings.exec_max_gas_gwei
        self.slippage_ceiling_bps = settings.exec_max_slippage_bps
        self.min_profit_usd = settings.exec_min_profit_usd
        self.max_size_usd = settings.exec_max_size_usd

        self.kill_switch = False
        self.kill_reason = ""
        self.dry_run = settings.dry_run

        self._outcomes: deque[TradeOutcome] = deque(maxlen=2000)
        self._latencies: deque[tuple[float, float]] = deque(maxlen=1000)
        self._exposure_chain: dict[int, Decimal] = {}
        self._exposure_token: dict[str, Decimal] = {}
        self._last_chain_seen: dict[int, float] = {}
        self._daily_reset_ts = time.time()

    # ------------------------------------------------------------------ #
    # Учёт событий
    # ------------------------------------------------------------------ #
    def record_outcome(self, outcome: TradeOutcome) -> None:
        """Фиксирует результат сделки и проверяет условия kill-switch."""
        self._outcomes.append(outcome)
        self._last_chain_seen[outcome.chain_id] = time.time()
        if outcome.status == "success":
            self._release_exposure(outcome)
        self._evaluate_auto_kill()

    def record_latency(self, latency_ms: float) -> None:
        """Фиксирует задержку (block -> bundle sent) для контроля p99."""
        self._latencies.append((time.time(), latency_ms))
        self._evaluate_auto_kill()

    def mark_chain_alive(self, chain_id: int) -> None:
        """Отмечает, что данные по сети приходят (для детектора разрыва связи)."""
        self._last_chain_seen[chain_id] = time.time()

    def reserve_exposure(self, chain_id: int, token: str, amount_usd: Decimal) -> None:
        """Резервирует экспозицию под заявку (снимается при успехе/провале)."""
        self._exposure_chain[chain_id] = self._exposure_chain.get(chain_id, Decimal("0")) + amount_usd
        self._exposure_token[token] = self._exposure_token.get(token, Decimal("0")) + amount_usd

    def _release_exposure(self, outcome: TradeOutcome) -> None:
        """Освобождает экспозицию по завершённой сделке (и по сети, и по токену)."""
        released = outcome.size_usd
        if released <= 0:
            return
        self._exposure_chain[outcome.chain_id] = max(
            self._exposure_chain.get(outcome.chain_id, Decimal("0")) - released, Decimal("0")
        )
        if outcome.token:
            self._exposure_token[outcome.token] = max(
                self._exposure_token.get(outcome.token, Decimal("0")) - released, Decimal("0")
            )

    # ------------------------------------------------------------------ #
    # Проверки
    # ------------------------------------------------------------------ #
    def check(
        self,
        *,
        chain_id: int,
        token_in: str,
        size_usd: Decimal,
        net_profit_usd: Decimal,
        base_fee_gwei: float,
        slippage_bps: int,
        protocol: str = "",
    ) -> RiskDecision:
        """Полная проверка сделки. Возвращает RiskDecision с деталями по каждому пункту."""
        decision = RiskDecision(allow=True)
        decision.checks = {
            "dry_run": self.dry_run,
            "kill_switch": self.kill_switch,
            "daily_pnl": str(self.daily_pnl()),
            "hourly_pnl": str(self.hourly_pnl()),
            "revert_rate_pct": round(self.revert_rate_pct(), 2),
        }

        if self.kill_switch:
            return decision.deny("kill_switch_active", kill_reason=self.kill_reason)
        if net_profit_usd < self.min_profit_usd:
            return decision.deny("below_min_profit", threshold=str(self.min_profit_usd))
        if size_usd > self.max_size_usd:
            return decision.deny("size_above_max", max_size=str(self.max_size_usd))
        if base_fee_gwei > float(self.gas_ceiling_gwei):
            return decision.deny("gas_above_ceiling", gas_ceiling=str(self.gas_ceiling_gwei))
        if slippage_bps > self.slippage_ceiling_bps:
            return decision.deny("slippage_above_ceiling", ceiling_bps=self.slippage_ceiling_bps)

        day_pnl = self.daily_pnl()
        if day_pnl <= -self.daily_loss_limit:
            return decision.deny("daily_loss_limit", pnl=str(day_pnl))
        hour_pnl = self.hourly_pnl()
        if hour_pnl <= -self.hourly_loss_limit:
            return decision.deny("hourly_loss_limit", pnl=str(hour_pnl))

        revert_rate = self.revert_rate_pct()
        if revert_rate > float(self.revert_rate_limit) and self._trade_count(window=300) >= 5:
            return decision.deny("revert_rate_limit", rate_pct=round(revert_rate, 2))

        chain_exposure = self._exposure_chain.get(chain_id, Decimal("0")) + size_usd
        if chain_exposure > self.max_exposure_chain:
            return decision.deny("chain_exposure_limit", exposure=str(chain_exposure))

        token_exposure = self._exposure_token.get(token_in, Decimal("0")) + size_usd
        if token_exposure > self.max_exposure_token:
            return decision.deny("token_exposure_limit", exposure=str(token_exposure))

        return decision

    def check_connection(self) -> RiskDecision:
        """Проверка «живости» источников данных (разрыв связи > connection_loss_sec)."""
        now = time.time()
        stale = [
            chain_id
            for chain_id, seen in self._last_chain_seen.items()
            if now - seen > self.connection_loss_sec
        ]
        decision = RiskDecision(allow=not stale)
        if stale:
            decision.reason = "stale_chain_feed"
            decision.checks["stale_chains"] = stale
        return decision

    # ------------------------------------------------------------------ #
    # Агрегаты
    # ------------------------------------------------------------------ #
    def daily_pnl(self) -> Decimal:
        """PnL за последние 24 часа (скользящее окно)."""
        return self._pnl_since(24 * 3600)

    def hourly_pnl(self) -> Decimal:
        """PnL за последний час."""
        return self._pnl_since(3600)

    def _pnl_since(self, seconds: float) -> Decimal:
        cutoff = time.time() - seconds
        return sum(
            (o.profit_usd for o in self._outcomes if o.ts >= cutoff and o.status == "success"),
            Decimal("0"),
        )

    def _trade_count(self, window: float) -> int:
        cutoff = time.time() - window
        return sum(1 for o in self._outcomes if o.ts >= cutoff)

    def revert_rate_pct(self, window: float = 300.0) -> float:
        """Доля ревертов за окно (по умолчанию 5 минут), в процентах."""
        cutoff = time.time() - window
        recent = [o for o in self._outcomes if o.ts >= cutoff]
        if not recent:
            return 0.0
        reverted = sum(1 for o in recent if o.status == "reverted")
        return reverted / len(recent) * 100

    def latency_p99_ms(self, window: float = 300.0) -> float:
        """p99 задержки «блок -> bundle отправлен» в миллисекундах."""
        cutoff = time.time() - window
        values = sorted(v for ts, v in self._latencies if ts >= cutoff)
        if not values:
            return 0.0
        idx = min(len(values) - 1, int(len(values) * 0.99))
        return values[idx]

    # ------------------------------------------------------------------ #
    # Kill-switch
    # ------------------------------------------------------------------ #
    def engage_kill_switch(self, reason: str) -> None:
        """Взводит kill-switch (вручную из UI или автоматически)."""
        self.kill_switch = True
        self.kill_reason = reason
        log.critical("KILL-SWITCH ENGAGED: %s", reason)

    def release_kill_switch(self, reason: str = "manual release") -> None:
        """Снимает kill-switch (только ручное действие оператора)."""
        self.kill_switch = False
        self.kill_reason = reason
        log.warning("kill-switch released: %s", reason)

    def _evaluate_auto_kill(self) -> None:
        """Автоматические условия остановки по ТЗ п.6."""
        if self.kill_switch:
            return
        if self.daily_pnl() <= -self.daily_loss_limit:
            self.engage_kill_switch("daily_loss_limit exceeded")
        elif self.hourly_pnl() <= -self.hourly_loss_limit:
            self.engage_kill_switch("hourly_loss_limit exceeded")
        elif self.revert_rate_pct() > float(self.revert_rate_limit) and self._trade_count(300) >= 10:
            self.engage_kill_switch("revert_rate exceeded")
        elif self.latency_p99_ms() > self.latency_p99_limit_ms > 0:
            self.engage_kill_switch("latency p99 exceeded")

    # ------------------------------------------------------------------ #
    def snapshot_state(self) -> dict[str, Any]:
        """Полное состояние риска для API/UI."""
        return {
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
            "dry_run": self.dry_run,
            "daily_pnl_usd": float(self.daily_pnl()),
            "hourly_pnl_usd": float(self.hourly_pnl()),
            "daily_limit_usd": float(self.daily_loss_limit),
            "hourly_limit_usd": float(self.hourly_loss_limit),
            "revert_rate_pct": round(self.revert_rate_pct(), 2),
            "revert_limit_pct": float(self.revert_rate_limit),
            "latency_p99_ms": round(self.latency_p99_ms(), 1),
            "latency_limit_ms": self.latency_p99_limit_ms,
            "gas_ceiling_gwei": float(self.gas_ceiling_gwei),
            "slippage_ceiling_bps": self.slippage_ceiling_bps,
            "min_profit_usd": float(self.min_profit_usd),
            "max_size_usd": float(self.max_size_usd),
            "exposure": {
                "chains": {str(k): float(v) for k, v in self._exposure_chain.items()},
                "tokens": {k: float(v) for k, v in self._exposure_token.items()},
            },
        }


_engine: RiskEngine | None = None


def get_risk_engine() -> RiskEngine:
    """Синглтон риск-движка (общий для orchestration-слоя и API)."""
    global _engine
    if _engine is None:
        _engine = RiskEngine()
    return _engine
