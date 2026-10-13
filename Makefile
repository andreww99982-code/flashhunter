# ============================================================================
# FlashHunter — корневой Makefile. Все основные команды проекта.
#   make help — список целей (самое полезное начало).
# ============================================================================
SHELL := /bin/bash
PY    := python3
PIP   := $(PY) -m pip

.PHONY: help install dev test test-all lint fmt rust-check rust-test contracts contracts-test \
        contracts-invariants deploy-local ui build up up-cluster up-observability down logs \
        preflight backtest migrate seed zip clean

help:           ## Показать список целей
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- установка --
install:        ## Установить Python-зависимости (dev) и UI-зависимости
	$(PIP) install -e "python[dev]"
	cd ui && npm install

# ------------------------------------------------------------------- запуск --
dev:            ## Локальный запуск API+UI (SQLite, мок-рынок) на http://localhost:8000
	$(PY) -m uvicorn flashhunter.api.app:app --app-dir python --host 0.0.0.0 --port 8000 --reload

ui:             ## Сборка панели (Vite -> python/flashhunter/ui_static)
	cd ui && npm run build

build:          ## Собрать Docker-образы (api + мониторинг)
	docker compose build

up:             ## Поднять стек (single-node: SQLite + мок-рынок) в фоне
	docker compose up -d

up-cluster:     ## Поднять стек с прод-хранилищами (Postgres + ClickHouse + Redis)
	docker compose --profile cluster up -d

up-observability: ## Поднять стек с метриками и дашбордами (Prometheus + Grafana)
	docker compose --profile cluster --profile observability up -d

down:           ## Остановить стек
	docker compose down

logs:           ## Логи стека
	docker compose logs -f --tail=200

# --------------------------------------------------------------------- тесты --
test:           ## Python-тесты (pytest)
	cd python && $(PY) -m pytest -q

test-all:       ## Полный прогон: pytest + UI-сборка + cargo + forge (что установлено)
	bash scripts/test-all.sh

lint:           ## ruff + mypy + eslint
	cd python && ruff check . && mypy flashhunter
	cd ui && npm run lint

fmt:            ## Форматирование Python и UI
	cd python && ruff format .
	cd ui && npm run format

rust-check:     ## cargo fmt + clippy (весь воркспейс, с -D warnings)
	cd rust && cargo fmt --all -- --check && cargo clippy --all-targets -- -D warnings

rust-test:      ## cargo test по всему Rust-воркспейсу
	cd rust && cargo test --workspace

# ---------------------------------------------------------------- контракты --
contracts:      ## Foundry: сборка + unit-тесты + фаззинг
	cd contracts && forge fmt --check && forge build && forge test -vv

contracts-test: ## Foundry: только тесты (быстро)
	cd contracts && forge test -vv

contracts-invariants: ## Инварианты Echidna (property-based фаззинг)
	cd contracts && echidna echidna/EchidnaFlashHunter.sol --contract EchidnaFlashHunter --config echidna/EchidnaConfig.yaml

deploy-local:   ## Деплой контрактов в локальный Anvil (адреса -> deployments/local.json)
	cd contracts && bash ../scripts/deploy-local.sh

# ------------------------------------------------------------------ операции --
preflight:      ## Проверки перед переходом в боевой режим (регламент ТЗ §16)
	bash scripts/preflight.sh

backtest:       ## Ночной сценарий: 500 блоков, профиль volatile, отчёт в /data/reports
	bash scripts/backtest.sh

migrate:        ## Применить SQL-миграции к выбранному хранилищу
	bash scripts/migrate.sh

seed:           ## Пересоздать базу и залить демо-данные (17 таблиц, 7 стратегий)
	bash scripts/seed.sh

zip:            ## Собрать релизный архив проекта (архив + инструкция по запуску)
	bash scripts/package.sh

clean:          ## Очистка артефактов сборки и кэшей
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache python/.pytest_cache data/*.db ui/dist dist-fh
