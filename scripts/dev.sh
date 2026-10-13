#!/usr/bin/env bash
# FlashHunter: локальная разработка без Docker.
#   * поднимает API (uvicorn --reload) на 0.0.0.0:8000;
#   * запускает Vite dev-сервер на 0.0.0.0:5173 с проксированием /api и /ws;
#   * при CTRL+C гасит оба процесса.
#
# Требуется: python3.12+, node 20+, установленные зависимости (make install).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export FLASHHUNTER_ENV="${FLASHHUNTER_ENV:-dev}"
export MOCK_MARKET_DATA="${MOCK_MARKET_DATA:-true}"
export FLASHHUNTER_DRY_RUN="${FLASHHUNTER_DRY_RUN:-true}"
export FLASHHUNTER_AUTO_OPEN_BROWSER="${FLASHHUNTER_AUTO_OPEN_BROWSER:-false}"
export SQLITE_PATH="${SQLITE_PATH:-/tmp/flashhunter_dev.db}"

PIDS=()
cleanup() {
  echo
  echo "Останавливаю процессы разработки..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ API:  http://localhost:8000  (SQLite: ${SQLITE_PATH})"
python3 -m uvicorn flashhunter.api.app:app \
  --app-dir python --host 0.0.0.0 --port 8000 --reload --log-level info &
PIDS+=($!)

echo "→ UI:   http://localhost:5173  (Vite dev, проксирует /api и /ws на :8000)"
(cd ui && npm run dev -- --host 0.0.0.0 --port 5173) &
PIDS+=($!)

echo
echo "Готово. Откройте http://localhost:5173 — логин admin/admin."
wait
