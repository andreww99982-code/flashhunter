#!/usr/bin/env bash
# FlashHunter: preflight-проверки перед переходом в БОЕВОЙ режим (регламент ТЗ §16).
#
# Запуск: bash scripts/preflight.sh
# Скрипт ничего не меняет — только проверяет и возвращает ненулевой код при рисках.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FAILURES=0
WARNINGS=0
ok()   { echo -e "\033[0;32m✔\033[0m $*"; }
warn() { echo -e "\033[0;33m!\033[0m $*"; WARNINGS=$((WARNINGS + 1)); }
fail() { echo -e "\033[0;31m✘\033[0m $*"; FAILURES=$((FAILURES + 1)); }

echo "=== FlashHunter preflight ==="
echo

# --- 1. Конфигурация -------------------------------------------------------- #
if [[ ! -f .env ]]; then
  fail ".env не найден: скопируйте .env.example и заполните значения"
else
  ok ".env найден"
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

# --- 2. Запрет приватных ключей в репозитории ------------------------------- #
echo
echo "--- Проверка утечки ключей (ADR 0006) ---"
KEY_HITS="$(grep -rIE "(PRIVATE_KEY|privateKey)\s*=\s*\"?0x[0-9a-fA-F]{64}" \
  --include='*.env' --include='*.json' --include='*.yaml' --include='*.yml' --include='*.py' \
  --exclude-dir=node_modules --exclude-dir=.git . 2>/dev/null | grep -v 'REPLACE_ME' | grep -v '0x0000' || true)"
if [[ -n "$KEY_HITS" ]]; then
  fail "Найден приватный ключ в файлах репозитория:"
  echo "$KEY_HITS" | head -5
else
  ok "Приватных ключей в репозитории не найдено"
fi

if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  fail ".env находится под контролем версий — уберите его из git (git rm --cached .env)"
else
  ok ".env не отслеживается git"
fi

# --- 3. Обязательные переменные для боевого режима -------------------------- #
echo
echo "--- Обязательные переменные ---"
check_var() {
  local name="$1"; local label="$2"
  local value="${!name:-}"
  if [[ -z "$value" || "$value" == *"REPLACE_ME"* ]]; then
    fail "${label} не задан (${name})"
  else
    ok "${label} задан"
  fi
}
check_var ETH_RPC_HTTP "RPC Ethereum"
check_var JWT_SECRET "JWT-секрет панели"
check_var FLASHBOTS_AUTH_KEY "Авторизация Flashbots"
check_var COLD_WALLET_ADDRESS "Адрес холодного кошелька (автосивип)"

if [[ "${JWT_SECRET:-}" == "change-me-in-production-please-32-chars" ]]; then
  fail "JWT_SECRET оставлен значением по умолчанию — сгенерируйте случайный (openssl rand -hex 32)"
fi

# --- 4. Риск-параметры ------------------------------------------------------ #
echo
echo "--- Риск-параметры (значения по умолчанию выглядят разумно) ---"
DAILY="${RISK_DAILY_LOSS_LIMIT_USD:-0}"
HOURLY="${RISK_HOURLY_LOSS_LIMIT_USD:-0}"
MIN_PROFIT="${EXEC_MIN_PROFIT_USD:-0}"
MAX_GAS="${EXEC_MAX_GAS_GWEI:-0}"

(( $(echo "$DAILY > 0" | bc -l 2>/dev/null || echo 1) )) && ok "Дневной лимит убытка: \$${DAILY}" || fail "Дневной лимит убытка не задан"
(( $(echo "$HOURLY > 0 && $HOURLY <= $DAILY" | bc -l 2>/dev/null || echo 1) )) \
  && ok "Часовой лимит убытка: \$${HOURLY}" \
  || warn "Часовой лимит (\$${HOURLY}) должен быть больше нуля и не больше дневного (\$${DAILY})"
(( $(echo "$MIN_PROFIT >= 1" | bc -l 2>/dev/null || echo 1) )) && ok "Минимальная прибыль сделки: \$${MIN_PROFIT}" || warn "Минимальная прибыль меньше \$1: газ съест маржу"
(( $(echo "$MAX_GAS <= 200" | bc -l 2>/dev/null || echo 1) )) && ok "Потолок газа: ${MAX_GAS} gwei" || warn "Потолок газа выше 200 gwei: риск переплатить на волатильности"

# --- 5. Первый запуск и 24-часовой dry-run ---------------------------------- #
echo
echo "--- Регламент обкатки ---"
if curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
  ok "API отвечает"
  DRY_RUN="$(curl -fsS http://127.0.0.1:8000/api/v1/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dry_run"))' 2>/dev/null || echo unknown)"
  BLOCKS="$(curl -fsS http://127.0.0.1:8000/api/v1/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("blocks_processed"))' 2>/dev/null || echo 0)"
  UPTIME="$(curl -fsS http://127.0.0.1:8000/api/v1/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uptime_sec"))' 2>/dev/null || echo 0)"
  echo "   dry_run=${DRY_RUN}, обработано блоков=${BLOCKS}, uptime=${UPTIME}s"

  if [[ "$DRY_RUN" != "True" && "$DRY_RUN" != "true" ]]; then
    warn "Бот уже в боевом режиме: убедитесь, что dry-run отработал 24 часа"
  fi
  if (( ${BLOCKS%%.*} < 1000 )); then
    fail "Обработано слишком мало блоков (${BLOCKS}): нужен прогон не менее 1000 блоков, желательно 24 ч"
  fi
  if (( ${UPTIME%%.*} < 86400 )); then
    warn "Uptime меньше 24 часов (${UPTIME}s): регламент требует суточного наблюдения перед боем"
  fi
else
  warn "API недоступен — пропускаю проверки рантайма (запустите: docker compose up -d)"
fi

# --- 6. Отчёт бэктеста ------------------------------------------------------ #
echo
echo "--- Последний бэктест ---"
if [[ -f data/reports/nightly.json ]]; then
  NET="$(python3 -c 'import json;print(json.load(open("data/reports/nightly.json")).get("total_net_usd", 0))' 2>/dev/null || echo 0)"
  if python3 -c "import sys; sys.exit(0 if float('${NET}') > 0 else 1)" 2>/dev/null; then
    ok "Ночной бэктест прибыльный: \$${NET}"
  else
    fail "Ночной бэктест убыточен (\$${NET}): не переключайтесь в боевой режим"
  fi
else
  warn "Отчёта бэктеста нет: выполните make backtest"
fi

# --- 7. Часы ----------------------------------------------------------------- #
echo
echo "--- Синхронизация времени ---"
OFFSET="$( (command -v chronyc >/dev/null && chronyc tracking | awk '/System time/ {print $4, $5, $6}') || echo "chrony недоступен" )"
echo "   offset: ${OFFSET}"
if [[ "$OFFSET" == *"chrony недоступен"* ]]; then
  warn "chrony не установлен: расхождение часов ломает дедлайны транзакций"
else
  ok "chrony контролирует время"
fi

# --- Итог ------------------------------------------------------------------- #
echo
echo "=== Итог: предупреждений ${WARNINGS}, блокирующих проблем ${FAILURES} ==="
if (( FAILURES > 0 )); then
  echo -e "\033[0;31mПереход в боевой режим ЗАПРЕЩЁН до устранения проблем выше.\033[0m"
  exit 1
fi
if (( WARNINGS > 0 )); then
  echo -e "\033[0;33mПроблем нет, но есть предупреждения — оцените их осознанно.\033[0m"
  exit 0
fi
echo -e "\033[0;32mПроверки пройдены. Помните: регламент требует 24 часа dry-run и малый первый депозит.\033[0m"
