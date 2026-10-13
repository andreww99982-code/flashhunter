#!/usr/bin/env bash
# FlashHunter: запуск «в одну команду» для локальной машины.
#
#   bash scripts/bootstrap.sh
#
# Что делает:
#   1) проверяет наличие docker и docker compose;
#   2) создаёт .env из .env.example и генерирует безопасные секреты (JWT, пароль админа);
#   3) собирает и поднимает стек (api + опционально хранилища/наблюдаемость);
#   4) ждёт готовности health-check и печатает адрес панели с учётными данными.
#
# Идемпотентен: повторный запуск не перезатирает уже существующий .env.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
umask 077

# --- цвета для читаемого вывода -------------------------------------------- #
C_RESET='\033[0m'; C_OK='\033[0;32m'; C_WARN='\033[0;33m'; C_ERR='\033[0;31m'; C_INFO='\033[0;36m'
ok()   { echo -e "${C_OK}✔${C_RESET} $*"; }
warn() { echo -e "${C_WARN}!${C_RESET} $*"; }
err()  { echo -e "${C_ERR}✘${C_RESET} $*" >&2; }
info() { echo -e "${C_INFO}→${C_RESET} $*"; }

# --- 1. Проверка окружения -------------------------------------------------- #
info "Проверяю зависимости..."
if ! command -v docker >/dev/null 2>&1; then
  err "Docker не установлен. На macOS установите Docker Desktop и повторите запуск."
  err "Инструкция: https://docs.docker.com/get-started/get-docker/"
  exit 1
fi
ok "docker: $(docker --version)"

if ! docker compose version >/dev/null 2>&1; then
  err "Docker Compose v2 недоступен (нужна команда 'docker compose', а не 'docker-compose')."
  exit 1
fi
ok "compose: $(docker compose version --short)"

if ! docker info >/dev/null 2>&1; then
  err "Не могу связаться с Docker. Запустите Docker Desktop (macOS) или Docker Engine и повторите запуск."
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  warn "openssl не найден: секреты сгенерирую из /dev/urandom"
fi

gen_secret() {
  local length="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$length"
  else
    head -c "$((length * 2))" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

replace_env() {
  local key="$1"
  local value="$2"
  local temp_file
  temp_file="$(mktemp .env.XXXXXX)"
  if sed "s|^${key}=.*$|${key}=${value}|" .env > "$temp_file"; then
    mv "$temp_file" .env
  else
    rm -f "$temp_file"
    return 1
  fi
}

# --- 2. Подготовка .env ----------------------------------------------------- #
if [[ -f .env ]]; then
  ok ".env уже существует — оставляю без изменений"
else
  info "Создаю .env из .env.example с автогенерацией секретов..."
  cp .env.example .env

  JWT_SECRET="$(gen_secret 32)"
  ADMIN_PASSWORD="fh-$(gen_secret 6)"
  POSTGRES_PASSWORD="$(gen_secret 12)"
  CLICKHOUSE_PASSWORD="$(gen_secret 12)"
  GRAFANA_PASSWORD="$(gen_secret 6)"

  # Заменяем только те строки, что оставлены пустыми/шаблонными в .env.example
  replace_env JWT_SECRET "$JWT_SECRET"
  replace_env ADMIN_DEFAULT_PASSWORD "$ADMIN_PASSWORD"
  replace_env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  replace_env CLICKHOUSE_PASSWORD "$CLICKHOUSE_PASSWORD"
  replace_env GRAFANA_PASSWORD "$GRAFANA_PASSWORD"

  ok ".env создан (JWT и пароли сгенерированы)"
  echo
  echo "──────────────────────────────────────────────────────────"
  echo " Учётные данные панели (сохраните!):"
  echo "   логин:  admin"
  echo "   пароль: ${ADMIN_PASSWORD}"
  echo " При первом входе система потребует сменить пароль."
  echo "──────────────────────────────────────────────────────────"
  echo "${JWT_SECRET}" > /dev/null   # JWT не печатаем: он не нужен оператору
fi

# --- 3. Запуск стека -------------------------------------------------------- #
PROFILES=()
if [[ "${FH_WITH_CLUSTER:-false}" == "true" ]]; then
  PROFILES+=(--profile cluster)
fi
if [[ "${FH_WITH_MONITORING:-false}" == "true" ]]; then
  PROFILES+=(--profile observability)
fi

info "Собираю образы (первый запуск занимает 3–6 минут: ставятся npm/pip зависимости)..."
docker compose "${PROFILES[@]}" build --quiet

info "Поднимаю контейнеры..."
docker compose "${PROFILES[@]}" up -d

# --- 4. Ожидание готовности ------------------------------------------------- #
info "Жду готовности API (health-check)..."
ATTEMPTS=60
for attempt in $(seq 1 "$ATTEMPTS"); do
  if curl -fsS "http://127.0.0.1:8000/api/v1/ping" >/dev/null 2>&1; then
    ok "API готов (попытка ${attempt}/${ATTEMPTS})"
    break
  fi
  if [[ "$attempt" == "$ATTEMPTS" ]]; then
    err "API не поднялся за $((ATTEMPTS * 2)) секунд. Посмотрите логи:"
    err "   docker compose logs --tail=100 api"
    exit 1
  fi
  sleep 2
done

HEALTH_JSON="$(curl -fsS http://127.0.0.1:8000/api/v1/health || echo '{}')"

echo
ok "FlashHunter запущен."
echo "  Панель:        http://localhost:8000"
echo "  API (docs):    http://localhost:8000/docs"
echo "  Метрики:       http://localhost:8000/metrics"
if [[ "${FH_WITH_MONITORING:-false}" == "true" ]]; then
  echo "  Prometheus:    http://localhost:9090"
  echo "  Grafana:       http://localhost:3001"
fi
echo
echo "  Статус: $(echo "$HEALTH_JSON" | head -c 300)"
echo
echo "Режим данных — реальная сеть; DRY-RUN не отправляет транзакции."
echo "После входа задайте собственный RPC и торговый кошелёк в панели настроек."
