# Миграции FlashHunter

Каталог содержит SQL-схемы для двух режимов хранения, поддерживаемых проектом:

| Режим | Каталог | Когда использовать |
|-------|---------|--------------------|
| **SQLite** (по умолчанию) | `sqlite/` | Локальный запуск, `docker compose up -d`, быстрый старт без внешних СУБД |
| **PostgreSQL + ClickHouse** (прод) | `postgres/`, `clickhouse/` | Продакшн: транзакционные данные в Postgres, телеметрия/аналитика в ClickHouse |

Режим выбирается переменной `FLASHHUNTER_DB_MODE` (`sqlite` | `postgres`).
В режиме `sqlite` миграции применяются автоматически при старте
(`flashhunter.db.session.init_models()` создаёт те же 17 таблиц).

## Применение

```bash
# SQLite — схема вручную (обычно не нужно: модели создаются при старте)
sqlite3 /data/flashhunter.db < sqlite/001_init.sql

# PostgreSQL (прод)
psql "$POSTGRES_DSN" -f postgres/001_init.sql
psql "$POSTGRES_DSN" -f postgres/002_timescale_and_views.sql

# ClickHouse (аналитика/латентность)
clickhouse-client --multiquery < clickhouse/001_init.sql
```

## Состав миграций

* `sqlite/001_init.sql` — все 17 таблиц ядра + индексы + сид-минимум.
* `postgres/001_init.sql` — те же таблицы с партиционированием по времени и `TIMESTAMPTZ`.
* `postgres/002_timescale_and_views.sql` — представления аналитики (PnL по дням, латентность по сервисам, конверсия релеев).
* `clickhouse/001_init.sql` — `MergeTree`-таблицы для телеметрии высокой частоты.

Схема соответствует моделям SQLAlchemy в `python/flashhunter/db/models.py` —
любое изменение моделей сопровождается миграцией.
