#!/usr/bin/env bash
# FlashHunter: сборка релизного архива проекта + краткая инструкция по запуску.
#   Итог: dist-fh/flashhunter-<версия>.zip  и  dist-fh/ЗАПУСК.md
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(python3 - <<'PY'
"""Версия берётся из pyproject, чтобы архив и пакет не расходились."""
import pathlib, re
text = pathlib.Path("python/pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
print(match.group(1) if match else "0.0.0")
PY
)"
STAMP="$(date -u +%Y%m%d)"
OUT_DIR="dist-fh"
ARCHIVE="${OUT_DIR}/flashhunter-${VERSION}-${STAMP}.zip"

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE"

echo "→ Собираю архив ${ARCHIVE} (версия ${VERSION})"

# Исключаем: зависимости, кэши, базы, .venv, локальные секреты — архив должен быть чистым
zip -qr "$ARCHIVE" . \
  -x "*/node_modules/*" \
  -x "*/.git/*" \
  -x "*/__pycache__/*" \
  -x "*/.pytest_cache/*" \
  -x "*/.mypy_cache/*" \
  -x "*/.ruff_cache/*" \
  -x "*/dist/*" \
  -x "*/target/*" \
  -x "*/.venv/*" \
  -x "*.db" -x "*.db-wal" -x "*.db-shm" \
  -x "*/.env" \
  -x "*/secrets/*" \
  -x "*.zip" \
  -x "*/data/reports/*" \
  -x "dist-fh/*" \
  -x "*/dist-fh/*" \
  -x "*/contracts/out/*" \
  -x "*/contracts/cache/*" \
  -x "*/echidna/corpus/*"

cat > "${OUT_DIR}/ЗАПУСК.md" <<'INSTRUCTIONS'
# FlashHunter: запуск

## Одной командой

```bash
unzip flashhunter-*.zip -d flashhunter
cd flashhunter
docker compose up -d
```

Готово: панель на **http://localhost:8000**, логин `admin` / `admin`
(система сразу потребует сменить пароль — это требование безопасности).

Если хотите автогенерацию секретов и проверку окружения:

```bash
bash scripts/bootstrap.sh
```

## Что вы увидите

* 12 страниц панели: дашборд, возможности, стратегии, контракты, кошельки,
  история, аналитика, латентность, настройки, журналы, бэктест, вход.
* Режим по умолчанию — **DRY-RUN с мок-рынком**: бот находит возможности,
  симулирует сделки, ведёт статистику, но не отправляет ничего в сеть.

## Дополнительные профили

```bash
# Прод-хранилища: Postgres + ClickHouse + Redis
docker compose --profile cluster up -d

# Метрики и дашборды: Prometheus + Grafana
docker compose --profile cluster --profile observability up -d

# Edge-прокси (nginx: gzip, rate-limit, TLS-терминация)
docker compose --profile edge up -d
```

## Полезные команды

```bash
make help            # список всех команд
make test            # Python-тесты (130 тестов)
make test-all        # pytest + UI-сборка + cargo + forge (что установлено)
make backtest        # бэктест 500 блоков, отчёты в data/reports/
make preflight       # проверки перед боевым режимом (регламент ТЗ §16)
make logs            # логи стека
make down            # остановить стек
```

## Боевой режим (только после обкатки!)

1. Заполните `.env`: RPC-ключи (`ETH_RPC_HTTP`), авторизацию релеев, `JWT_SECRET`,
   адрес холодного кошелька.
2. Прогоните 24 часа в dry-run и проверьте отчёты: `make backtest`, страницы
   `/analytics`, `/latency`, `/history`.
3. Выполните `make preflight` — он заблокирует переход, если что-то не готово.
4. Установите `FLASHHUNTER_DRY_RUN=false` и перезапустите: `docker compose up -d`.
5. Начните с малого баланса keeper-кошелька (рекомендуется $50–100 на первые сутки).

## Документация в архиве

| Файл | О чём |
|------|-------|
| `README.md` | Полное описание проекта, страниц, стратегий, API |
| `docs/architecture.md` | Архитектура, потоки данных, бюджеты латентности, точки отказа |
| `docs/adr/*.md` | Почему приняты ключевые решения (8 ADR) |
| `proto/flashhunter.proto` | gRPC-контракты внутренней шины |
| `migrations/` | Схемы SQLite / Postgres / ClickHouse |
| `contracts/README.md` | Контракты, Foundry-тесты, инварианты Echidna |
| `infra/{docker,k8s,terraform,ansible}` | Пром-развёртывание |

## Этическая политика

Проект реализует только «честный MEV»: арбитраж, ликвидации, JIT-ликвидность,
бэкран, исполнение интентов. Сэндвич-атаки, эксплойты, атаки на других ботов,
обход KYC/AML и обфускация кода — **вне области проекта**
(`docs/adr/0005-no-sandwich-policy.md`).
INSTRUCTIONS

# Отдельно положим инструкцию и внутрь архива (на случай распаковки «как есть»)
cp "${OUT_DIR}/ЗАПУСК.md" "${OUT_DIR}/_instructions.tmp"
zip -qj "$ARCHIVE" "${OUT_DIR}/ЗАПУСК.md" -x "" || true
rm -f "${OUT_DIR}/_instructions.tmp"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "✔ Архив готов: ${ARCHIVE} (${SIZE})"
echo "✔ Инструкция:  ${OUT_DIR}/ЗАПУСК.md"
echo
echo "Проверка содержимого (первые 12 записей):"
unzip -l "$ARCHIVE" | head -16
