#!/usr/bin/env bash
# FlashHunter: ночной сценарий бэктеста (тот же код, что в CronJob k8s).
# Прогоняет 500 блоков на волатильном профиле и сохраняет отчёты в data/reports/.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BLOCKS="${1:-500}"
SEED="${2:-20261012}"
PROFILE="${3:-volatile}"

mkdir -p data/reports
export SQLITE_PATH="${SQLITE_PATH:-/tmp/flashhunter_backtest.db}"
export MOCK_MARKET_DATA=true

echo "→ Бэктест: блоков=${BLOCKS}, seed=${SEED}, профиль=${PROFILE}"

python3 - "$BLOCKS" "$SEED" "$PROFILE" <<'PY'
"""Прогон бэктеста и сохранение отчётов (JSON + CSV для Excel/BI)."""
import asyncio
import json
import pathlib
import sys

from flashhunter.backtest.engine import BacktestEngine
from flashhunter.db.session import init_models

blocks, seed, profile = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]


async def main() -> None:
    # 1. Готовим БД: бэктест пишет найденные возможности и сделки в те же таблицы
    await init_models()
    engine = BacktestEngine(
        strategies=["dex_arb", "triangular", "stable_peg", "liquidation", "jit", "backrun", "intent"],
        profile=profile,
        seed=seed,
        min_profit_usd=5.0,
    )
    report = engine.run_mock(blocks=blocks)
    data = report.as_dict()

    reports = pathlib.Path("data/reports")
    (reports / "nightly.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports / "nightly.csv").write_text(report.to_csv(), encoding="utf-8-sig")

    # 2. Печать сводки для человека и для CI-логов
    print(f"Блоков:            {data['blocks']}")
    print(f"Скорость:          {data['blocks_per_sec']} бл/с")
    print(f"Найдено возможн.:  {data['total_found']}")
    print(f"Итоговый PnL:      ${data['total_net_usd']:,.2f}")
    print(f"Per block avg:     ${data['avg_per_block_usd']:,.2f}")
    print("По стратегиям:")
    for name, row in sorted(data["by_strategy"].items(), key=lambda kv: -kv[1].get("net_usd", 0)):
        print(f"  {name:<12} найдено={row.get('found', 0):<5} исполнено={row.get('executed', 0):<4} net=${row.get('net_usd', 0):,.2f}")
    print("Отчёты: data/reports/nightly.json, data/reports/nightly.csv")


asyncio.run(main())
PY
