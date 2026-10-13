"""Треугольный (multi-hop) арбитраж: A -> B -> C -> ... -> A, до 4 хопов.

Граф строится по снимку пулов: узел — токен, ребро — пул в конкретном направлении.
Поиск циклов — ограниченный по глубине DFS от «якорных» токенов (WETH/USDC),
что соответствует реальной практике: циклы без якоря почти всегда неликвидны.

Оптимизация размера — тернарный поиск по составной функции прибыли
(композиция выходов нескольких AMM). Для 3 хопов на V2 функция также вогнута,
поэтому поиск даёт глобальный оптимум.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from flashhunter.market.snapshot import MarketSnapshot, PoolSnapshot
from flashhunter.strategies.base import BaseStrategy, BlockCtx, Opportunity, gas_cost_usd
from flashhunter.strategies.math.adapters import amount_out, pool_spot_price, probe_amount
from flashhunter.strategies.math.amm import gradient_ascent_optimal_size, ternary_search_optimal_size

log = logging.getLogger(__name__)

ANCHOR_SYMBOLS = {"WETH", "USDC", "USDT", "WBTC", "BNB", "POL"}


@dataclass(slots=True)
class Edge:
    """Ребро графа: пул + направление свопа."""

    pool: PoolSnapshot
    token_in: str
    token_out: str


class TriangularStrategy(BaseStrategy):
    """Поиск прибыльных циклов длиной 3–4 хопа по графу токенов."""

    name = "triangular"  # type: ignore[assignment]
    chains = [1, 8453, 42161]

    def __init__(self, params: dict | None = None, chains: list[int] | None = None) -> None:
        super().__init__(params, chains)
        self.min_profit_usd = Decimal(str(self.param("min_profit_usd", 10.0)))
        self.min_size_usd = Decimal(str(self.param("min_size_usd", 800.0)))
        self.max_size_usd = Decimal(str(self.param("max_size_usd", 150_000.0)))
        self.max_hops = int(self.param("max_hops", 4))
        self.max_cycles = int(self.param("max_cycles", 120))
        self.gas_units = int(self.param("gas_units", 420_000))
        self.top_n = int(self.param("top_n", 8))
        self.use_gradient = bool(self.param("use_gradient", True))
        self.gradient_iterations = int(self.param("iterations", 24))
        # Порог отсева: во сколько раз произведение спотовых цен по циклу должно
        # превышать 1, чтобы мы вообще считали размер. 1.0005 = 5 bps запаса —
        # этого хватает на комиссии двух-трёх свопов и газ, но отсекает «плоские» циклы.
        self.max_roi_pct = float(self.param("max_roi_pct", 25.0))
        self.screen_edge = float(self.param("screen_edge", 0.0005))
        # Бюджет поиска циклов (мс) и ограничение графа: защита от комбинаторного взрыва
        self.search_budget_ms = float(self.param("search_budget_ms", 120.0))
        self.max_pools_per_token = int(self.param("max_pools_per_token", 6))
        self.min_pool_tvl_usd = float(self.param("min_pool_tvl_usd", 50_000.0))
        self.last_screened = 0

    # ------------------------------------------------------------------ #
    async def on_block(self, ctx: BlockCtx) -> list[Opportunity]:
        """Строит граф, ищет циклы и оптимизирует размер для каждого."""
        graph = self._build_graph(ctx.snapshot)
        cycles = self._find_cycles(ctx.snapshot, graph)
        results: list[Opportunity] = []
        screened = 0
        for cycle in cycles[: self.max_cycles]:
            # Отсев до численной оптимизации: если произведение спотовых цен по циклу
            # не оставляет запаса хотя бы на комиссии, оптимизировать размер бессмысленно.
            # На живом рынке это убирает почти все циклы и ускоряет блок в десятки раз.
            spot = 1.0
            for edge in cycle:
                price = pool_spot_price(edge.pool, edge.token_in)
                if price <= 0:
                    spot = 0.0
                    break
                spot *= price
            if spot <= 1.0 + self.screen_edge:
                screened += 1
                continue
            opp = self._evaluate(ctx, cycle)
            if opp is not None:
                results.append(opp)
        self.last_screened = screened
        results.sort(key=lambda o: o.net_profit_usd, reverse=True)
        return results[: self.top_n]

    # ------------------------------------------------------------------ #
    def _build_graph(self, snapshot: MarketSnapshot) -> dict[str, list[Edge]]:
        """Список смежности: token_in -> [рёбра], не более N самых ликвидных пулов.

        Полный граф из 80+ пулов даёт сотни тысяч циклов: перебирать их бессмысленно
        (в них нет денег — мелкие пулы), а время блока тратится впустую. Поэтому в
        граф попадают только пулы с наибольшим TVL по каждому токену.
        """
        candidates: dict[str, list[PoolSnapshot]] = {}
        for pool in snapshot.pools.values():
            if pool.chain_id != snapshot.chain_id:
                continue
            candidates.setdefault(pool.token0.lower(), []).append(pool)
            candidates.setdefault(pool.token1.lower(), []).append(pool)

        allowed: set[str] = set()
        for token, pools in candidates.items():
            pools.sort(key=lambda p: -float(p.tvl_usd))
            allowed.update(p.address for p in pools[: self.max_pools_per_token])

        graph: dict[str, list[Edge]] = {}
        for pool in snapshot.pools.values():
            if pool.chain_id != snapshot.chain_id or pool.address not in allowed:
                continue
            if float(pool.tvl_usd) < self.min_pool_tvl_usd:
                continue
            graph.setdefault(pool.token0.lower(), []).append(
                Edge(pool=pool, token_in=pool.token0.lower(), token_out=pool.token1.lower())
            )
            graph.setdefault(pool.token1.lower(), []).append(
                Edge(pool=pool, token_in=pool.token1.lower(), token_out=pool.token0.lower())
            )
        # Рёбра внутри токена — по убыванию TVL: сначала самые «денежные» пути
        for edges in graph.values():
            edges.sort(key=lambda e: -float(e.pool.tvl_usd))
        return graph

    def _find_cycles(
        self, snapshot: MarketSnapshot, graph: dict[str, list[Edge]]
    ) -> list[list[Edge]]:
        """Ограниченный DFS: ищем циклы, начинающиеся и заканчивающиеся в якорном токене."""
        anchors = [
            info.address.lower()
            for info in snapshot.tokens.values()
            if info.symbol in ANCHOR_SYMBOLS and info.address.lower() in graph
        ]
        cycles: list[list[Edge]] = []
        seen: set[tuple[str, ...]] = set()

        deadline = time.perf_counter() + self.search_budget_ms / 1000
        used_pools: set[str] = set()

        def dfs(start: str, current: str, path: list[Edge], visited: set[str]) -> None:
            # Жёсткие границы поиска: время блока дороже полноты перебора.
            # Без них DFS по 80 пулам порождает сотни тысяч циклов и один блок
            # «съедает» больше секунды — на живом рынке это означает проигрыш гонки.
            if len(cycles) >= self.max_cycles or time.perf_counter() > deadline:
                return
            if len(path) > self.max_hops:
                return
            for edge in graph.get(current, []):
                if edge.pool.address in {p.pool.address for p in path}:
                    continue  # пул в цикле не повторяется
                if edge.token_out == start and len(path) >= 2:
                    cycle = (*path, edge)
                    key = tuple(sorted(e.pool.address for e in cycle))
                    if key not in seen:
                        seen.add(key)
                        cycles.append(list(cycle))
                        used_pools.update(e.pool.address for e in cycle)
                    continue
                if edge.token_out in visited:
                    continue
                dfs(start, edge.token_out, [*path, edge], visited | {edge.token_out})

        for anchor in anchors[:4]:
            dfs(anchor, anchor, [], {anchor})
            if len(cycles) >= self.max_cycles or time.perf_counter() > deadline:
                break
        # Циклы, не прошедшие отсев по времени, отбрасываем сразу: лучше 120 честно
        # проверенных маршрутов, чем миллион непроверенных.
        return cycles[: self.max_cycles]

    def _evaluate(self, ctx: BlockCtx, cycle: list[Edge]) -> Opportunity | None:
        """Оптимизирует размер и считает прибыль конкретного цикла."""
        snapshot = ctx.snapshot
        token_in = cycle[0].token_in
        token_out = cycle[0].token_out  # токен, который получаем на первом хопе
        min_size_units = probe_amount(cycle[0].pool, snapshot, token_in, self.min_size_usd)
        max_size_units = probe_amount(cycle[0].pool, snapshot, token_in, self.max_size_usd)
        if min_size_units <= 0 or max_size_units <= min_size_units:
            return None

        def final_amount(dx: int) -> int:
            """Прокатывает dx через весь цикл."""
            amount = dx
            for edge in cycle:
                amount = amount_out(edge.pool, edge.token_in, amount)
                if amount <= 0:
                    return 0
            return amount

        def profit_fn(dx: int) -> float:
            return float(final_amount(dx) - dx)

        if self.use_gradient and len(cycle) > 3:
            # для длинных циклов — градиентный подъём из разумной стартовой точки
            start = float(min(max_size_units // 8, 1))
            best_x = gradient_ascent_optimal_size(
                lambda x: profit_fn(int(max(x, 1.0))),
                start,
                max_iterations=self.gradient_iterations,
                lower=float(min_size_units),
                upper=float(max_size_units),
            )
            size = int(best_x)
            # локальная доводка тернарным поиском
            lo = max(min_size_units, size * 3 // 4)
            hi = min(max_size_units, max(size * 5 // 4, lo + 1))
            size = ternary_search_optimal_size(profit_fn, lo, hi, iterations=32)
        else:
            size = ternary_search_optimal_size(profit_fn, min_size_units, max_size_units, iterations=44)

        out_amount = final_amount(size)
        gross_profit = out_amount - size
        if gross_profit <= 0:
            return None

        size_usd = snapshot.to_usd(token_in, size)
        profit_usd = snapshot.to_usd(token_in, gross_profit)
        gas_usd = gas_cost_usd(
            gas_units=self.gas_units,
            base_fee_gwei=ctx.base_fee_gwei,
            priority_fee_gwei=float(self.param("priority_fee_gwei", 2.0)),
            native_price_usd=snapshot.native_price_usd,
        )
        flash_fee_usd = size_usd * Decimal(5) / Decimal(10_000)  # Aave V3 0.05%
        net = profit_usd - gas_usd - flash_fee_usd
        if net < self.min_profit_usd:
            return None

        roi = float(net / size_usd * 100) if size_usd > 0 else 0.0
        # Аномальная доходность за один блок — признак ошибки данных (битый пул,
        # неверные decimals, токен-приманка). По такой «возможности» нельзя тратить
        # реальные деньги: трёхногая цепочка обязана укладываться в единицы процентов.
        if roi > self.max_roi_pct or roi <= 0 or size_usd <= 0:
            return None
        confidence = min(0.97, 0.4 + min(roi, 1.0) * 0.3 + 0.05 * len(cycle))
        symbols = [snapshot.token(e.token_in).symbol for e in cycle] + [
            snapshot.token(cycle[-1].token_out).symbol
        ]

        return Opportunity(
            strategy=self.name,
            chain_id=ctx.chain_id,
            token_in=token_in,
            token_out=token_out,
            size_in=size,
            expected_out=out_amount,
            profit_value=Decimal(gross_profit),
            profit_usd=profit_usd,
            gas_usd=gas_usd,
            flash_fee_usd=flash_fee_usd,
            confidence=confidence,
            block_number=ctx.block_number,
            dex_buy=cycle[0].pool.dex,
            dex_sell=cycle[-1].pool.dex,
            pools=[e.pool.address for e in cycle],
            tokens=[e.token_in for e in cycle],
            kind="cycle",
            meta={
                "size_usd": str(size_usd),
                "hops": len(cycle),
                "route": " -> ".join(symbols),
                "roi_pct": roi,
            },
        )
