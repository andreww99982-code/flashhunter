# Скрипты FlashHunter

Все скрипты идемпотентны и не меняют ничего за пределами своего сценария.
Запускать из корня репозитория.

| Скрипт | Назначение | Пример |
|--------|-----------|--------|
| `bootstrap.sh` | Первый запуск: проверка Docker, генерация `.env` со случайными секретами, сборка и подъём стека, ожидание health-check | `bash scripts/bootstrap.sh` |
| `dev.sh` | Локальная разработка без Docker: API (uvicorn --reload) + Vite dev с проксированием | `bash scripts/dev.sh` |
| `preflight.sh` | Проверки перед боевым режимом: ключи в репозитории, обязательные переменные, лимиты риска, uptime, отчёт бэктеста, синхронизация часов | `bash scripts/preflight.sh` |
| `backtest.sh` | Бэктест N блоков с сохранением отчётов в `data/reports/` | `bash scripts/backtest.sh 500 42 volatile` |
| `migrate.sh` | Применение SQL-миграций (sqlite / postgres / clickhouse) | `bash scripts/migrate.sh postgres` |
| `seed.sh` | Пересоздание локальной БД с демо-данными (защита от запуска на прод-пути) | `bash scripts/seed.sh` |
| `test-all.sh` | Полный прогон: pytest, ruff+mypy, сборка UI, cargo test, forge test, валидация compose | `bash scripts/test-all.sh` |
| `deploy-local.sh` | Деплой контрактов в локальный Anvil и запись адресов в `deployments/local.json` | `bash scripts/deploy-local.sh` |
| `package.sh` | Сборка релизного архива `dist-fh/flashhunter-<версия>-<дата>.zip` + инструкция «ЗАПУСК.md» | `bash scripts/package.sh` |

## Через Makefile

```bash
make up          # = docker compose up -d
make preflight   # = bash scripts/preflight.sh
make backtest    # = bash scripts/backtest.sh
make test-all    # = bash scripts/test-all.sh
make zip         # = bash scripts/package.sh
```

## Безопасность скриптов

* `seed.sh` отказывается работать, если `SQLITE_PATH` указывает вне `/tmp` и `./data`
  (защита от случайного удаления прод-базы).
* `preflight.sh` завершается кодом 1 при блокирующих проблемах — его можно
  использовать как шаг в CD-пайплайне.
* `deploy-local.sh` использует только публично известный тестовый ключ Anvil;
  реальные ключи скрипты не читают и не пишут.
