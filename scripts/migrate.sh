#!/usr/bin/env bash
# FlashHunter: применение SQL-миграций к выбранному хранилищу.
#
#   bash scripts/migrate.sh sqlite   # схема в файл SQLite (обычно делает приложение)
#   bash scripts/migrate.sh postgres # 17 таблиц + партиции + вьюхи аналитики
#   bash scripts/migrate.sh clickhouse
#
# DSN берётся из .env (POSTGRES_DSN / CLICKHOUSE_DSN) или из переменных окружения.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-sqlite}"
[[ -f .env ]] && { set -a; # shellcheck disable=SC1091
  source .env; set +a; }

echo "→ Применяю миграции: ${MODE}"

case "$MODE" in
  sqlite)
    DB_PATH="${SQLITE_PATH:-/tmp/flashhunter.db}"
    mkdir -p "$(dirname "$DB_PATH")"
    if ! command -v sqlite3 >/dev/null 2>&1; then
      echo "sqlite3 CLI не найден. Схема всё равно создаётся приложением при старте:"
      echo "  python3 -c 'import asyncio; from flashhunter.db.session import init_models; asyncio.run(init_models())'"
      exit 0
    fi
    sqlite3 "$DB_PATH" < migrations/sqlite/001_init.sql
    echo "✔ Схема применена к ${DB_PATH}"
    sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" | sed 's/^/   таблица: /'
    ;;

  postgres)
    if ! command -v psql >/dev/null 2>&1; then
      echo "psql не найден. Применяю через контейнер postgres из compose..."
      docker compose --profile cluster exec -T postgres \
        psql -U "${POSTGRES_USER:-flashhunter}" -d "${POSTGRES_DB:-flashhunter}" \
        < migrations/postgres/001_init.sql
      docker compose --profile cluster exec -T postgres \
        psql -U "${POSTGRES_USER:-flashhunter}" -d "${POSTGRES_DB:-flashhunter}" \
        < migrations/postgres/002_timescale_and_views.sql
    else
      DSN="${POSTGRES_DSN:-postgresql://flashhunter:flashhunter@localhost:5432/flashhunter}"
      # asyncpg-схема DSN не понимает psql — приводим к обычному виду
      CLEAN_DSN="${DSN/postgresql+asyncpg:\/\//postgresql://}"
      psql "$CLEAN_DSN" -v ON_ERROR_STOP=1 -f migrations/postgres/001_init.sql
      psql "$CLEAN_DSN" -v ON_ERROR_STOP=1 -f migrations/postgres/002_timescale_and_views.sql
    fi
    echo "✔ Postgres: таблицы, партиции и вьюхи аналитики применены"
    ;;

  clickhouse)
    if command -v clickhouse-client >/dev/null 2>&1; then
      clickhouse-client --multiquery < migrations/clickhouse/001_init.sql
    else
      docker compose --profile cluster exec -T clickhouse \
        clickhouse-client --multiquery < migrations/clickhouse/001_init.sql
    fi
    echo "✔ ClickHouse: MergeTree-таблицы телеметрии и materialized view созданы"
    ;;

  *)
    echo "Неизвестный режим: ${MODE}. Доступно: sqlite | postgres | clickhouse" >&2
    exit 2
    ;;
esac
