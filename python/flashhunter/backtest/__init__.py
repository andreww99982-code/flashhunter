"""Бэктест-движок: прогон стратегий на мок-рынке или на исторических данных ClickHouse."""

from flashhunter.backtest.engine import BacktestEngine, BacktestReport, StrategyReport

__all__ = ["BacktestEngine", "BacktestReport", "StrategyReport"]
