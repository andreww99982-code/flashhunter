#!/usr/bin/env bash
# FlashHunter: пересоздание локальной базы с демо-данными.
#   * удаляет файл SQLite (только в /tmp или ./data — прод-адреса не трогает!);
#   * создаёт 17 таблиц через модели SQLAlchemy;
#   * заливает сид: админ, 15 сетей, 75 токенов, 7 стратегий, лимиты риска, контракты, кошелёк.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="${SQLITE_PATH:-/tmp/flashhunter_seed.db}"

# Защита: не даём случайно снести прод-базу на смонтированном томе
case "$DB_PATH" in
  /tmp/*|./data/*|data/*) ;;
  *) echo "Отказ: SQLITE_PATH=${DB_PATH} не в безопасном месте (разрешены /tmp и ./data)." >&2
     echo "Установите SQLITE_PATH=/tmp/flashhunter.db и повторите." >&2
     exit 2 ;;
esac

echo "→ Пересоздаю базу: ${DB_PATH}"
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"

export SQLITE_PATH="$DB_PATH"
export MOCK_MARKET_DATA="${MOCK_MARKET_DATA:-true}"
export FLASHHUNTER_DRY_RUN="${FLASHHUNTER_DRY_RUN:-true}"

python3 - <<'PY'
"""Заполнение демо-данными: тот же путь, что выполняет приложение при старте."""
import asyncio
import os

from flashhunter.db.seed import seed_all
from flashhunter.db.session import init_models


async def main() -> None:
    # 1. Схема: 17 таблиц (users, networks, tokens, pools, opportunities, trades, ...)
    await init_models()
    # 2. Сид: админ admin/admin, 15 сетей, 75 токенов, 7 стратегий, 13 лимитов риска
    stats = await seed_all()
    print("✔ Сид залит:", stats)
    # 3. Напоминание про смену пароля: это требование ТЗ, а не декорация
    print("  Логин: admin / admin → система потребует сменить пароль при первом входе")


asyncio.run(main())
PY

echo "✔ База готова: ${DB_PATH}"
echo "  Проверить: SQLITE_PATH=${DB_PATH} python3 -m uvicorn flashhunter.api.app:app --app-dir python --port 8000"
