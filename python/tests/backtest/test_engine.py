"""Тесты бэктест-движка: прогон, детерминизм, метрики, grid-search, CSV."""

from __future__ import annotations

from flashhunter.backtest.engine import BacktestEngine, summarize_curve
from flashhunter.market.mock import MockMarketSource


def test_mock_run_produces_report_with_curve() -> None:
    """Прогон на мок-рынке возвращает отчёт с кривой капитала длиной в число блоков."""
    engine = BacktestEngine(strategies=["dex_arb", "triangular"], profile="volatile", seed=3)
    report = engine.run_mock(blocks=30)

    assert report.blocks == 30
    assert len(report.equity_curve) == 30
    assert len(report.opportunities_per_block) == 30
    assert report.total_found > 0
    assert report.duration_sec > 0
    assert report.blocks_per_sec > 0


def test_report_is_deterministic_for_same_seed() -> None:
    """Один и тот же seed даёт идентичный отчёт (сравнение параметров корректно)."""
    first = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=7).run_mock(blocks=20)
    second = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=7).run_mock(blocks=20)
    assert first.total_found == second.total_found
    assert first.equity_curve == second.equity_curve


def test_calm_profile_is_quieter_than_stress() -> None:
    """Стресс-профиль даёт больше возможностей и больше прибыли, чем спокойный."""
    calm = BacktestEngine(strategies=["dex_arb", "triangular"], profile="calm", seed=11).run_mock(blocks=30)
    stress = BacktestEngine(strategies=["dex_arb", "triangular"], profile="stress", seed=11).run_mock(blocks=30)
    assert stress.total_found > calm.total_found
    assert stress.total_net_usd >= calm.total_net_usd


def test_min_profit_filter_reduces_executions() -> None:
    """Повышение порога минимальной прибыли уменьшает число исполненных сделок."""
    low = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=5, min_profit_usd=1).run_mock(blocks=25)
    high = BacktestEngine(
        strategies=["dex_arb"], profile="volatile", seed=5, min_profit_usd=100_000
    ).run_mock(blocks=25)
    executed_low = sum(item.executed for item in low.strategies)
    executed_high = sum(item.executed for item in high.strategies)
    assert executed_low > executed_high == 0


def test_fill_ratio_scales_profit_linearly() -> None:
    """Доля «долетевших» сделок линейно влияет на итоговую прибыль."""
    always = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=9, fill_ratio=1.0).run_mock(blocks=30)
    half = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=9, fill_ratio=0.5).run_mock(blocks=30)
    assert always.total_net_usd > half.total_net_usd


def test_replay_uses_provided_snapshots() -> None:
    """Режим реплея прогоняет ровно те снимки, что переданы (для данных ClickHouse)."""
    source = MockMarketSource(seed=21, profile="normal")
    snapshots = [source.next_snapshot() for _ in range(7)]
    engine = BacktestEngine(strategies=["dex_arb"], profile="normal", seed=21)
    report = engine.run_replay(snapshots)
    assert report.blocks == 7
    assert len(report.equity_curve) == 7


def test_unknown_strategy_rejected() -> None:
    """Неизвестная стратегия — ошибка конфигурации прогона."""
    import pytest

    with pytest.raises(ValueError):
        BacktestEngine(strategies=["sandwich_attack"])


def test_csv_export_writes_header_and_rows(tmp_path) -> None:
    """CSV-выгрузка отчёта содержит заголовок и строки по стратегиям."""
    report = BacktestEngine(strategies=["dex_arb", "liquidation"], profile="volatile", seed=4).run_mock(blocks=20)
    path = report.to_csv(tmp_path / "report.csv")
    text = path.read_text(encoding="utf-8-sig")
    assert text.startswith("strategy;found;executed")
    assert "dex_arb" in text and "liquidation" in text


def test_grid_search_returns_sorted_results() -> None:
    """Grid-search возвращает комбинации параметров, отсортированные по метрике."""
    engine = BacktestEngine(strategies=["dex_arb"], profile="volatile", seed=2)
    results = engine.grid_search({"min_profit_usd": [5, 50], "fill_ratio": [0.5, 1.0]}, blocks=15)
    assert len(results) == 4
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    assert engine.min_profit_usd == __import__("decimal").Decimal("10")  # параметры восстановлены


def test_summarize_curve_metrics() -> None:
    """Метрики кривой: финал, максимальная просадка, волатильность, Sharpe."""
    stats = summarize_curve([0.0, 10.0, 5.0, 25.0, 40.0])
    assert stats["final"] == 40.0
    assert stats["max_drawdown"] == 5.0  # 10 -> 5
    assert stats["volatility"] > 0
    assert stats["sharpe"] > 0


def test_summarize_empty_curve() -> None:
    """Пустая кривая не приводит к делению на ноль."""
    assert summarize_curve([]) == {"final": 0.0, "max_drawdown": 0.0, "volatility": 0.0, "sharpe": 0.0}


def test_report_as_dict_matches_api_contract() -> None:
    """as_dict() — контракт ответа POST /api/v1/backtest/run."""
    report = BacktestEngine(strategies=["dex_arb"], profile="normal", seed=1).run_mock(blocks=10)
    payload = report.as_dict()
    for key in (
        "blocks",
        "seed",
        "profile",
        "duration_sec",
        "blocks_per_sec",
        "total_found",
        "total_net_usd",
        "avg_per_block_usd",
        "by_strategy",
        "equity_curve",
        "note",
    ):
        assert key in payload
    assert payload["by_strategy"][0]["strategy"] == "dex_arb"
