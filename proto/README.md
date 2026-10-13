# Proto: внутренние gRPC-контракты FlashHunter

Единый источник истины для структур, которыми обмениваются Rust-сервисы
и Python-orchestrator: блоки, состояния пулов, возможности, вердикты риска,
результаты симуляции, отправки в релеи, интенты.

## Зачем отдельный proto

1. **Скорость горячего пути.** JSON-парсинг на каждом блоке — это десятки микросекунд,
   которых нет в бюджете 5000 мкс у ингестора. Protobuf дешевле и строго типизирован.
2. **Один контракт — два рантайма.** Rust и Python генерируют типы из одного файла,
   поэтому «поле переименовали только в одном сервисе» невозможно.
3. **Версионирование.** Пакет `flashhunter.v1`: ломающие изменения идут в `v2`,
   старые и новые сервисы могут работать рядом во время деплоя.

## Кодогенерация

```bash
# Python (grpcio-tools)
python -m grpc_tools.protoc -I proto \
  --python_out=python/flashhunter/proto \
  --grpc_python_out=python/flashhunter/proto \
  proto/flashhunter.proto

# Rust (tonic-build) — build.rs в соответствующем крейте
cargo build -p fh-proto

# Проверка совместимости с предыдущей версией
buf breaking --against '.git#branch=main' proto/
```

## Сервисы

| Сервис | Кто реализует | Назначение |
|--------|---------------|-----------|
| `IngestorService` | Rust `fh-ingestor` | Публикация блоков и событий пулов |
| `PoolIndexerService` | Rust `fh-pool-indexer` | Выдача свежих состояний пулов |
| `SimEngineService` | Rust `fh-sim-engine` | Локальная симуляция маршрута (revm) |
| `RelayService` | Rust `fh-relay-client` | Сборка bundle и fan-out по релеям |
| `RiskService` | Python `flashhunter.risk` | Право вето, kill-switch, лимиты |
| `AlertService` | Python `flashhunter.alerting` | Telegram/Discord/webhook уведомления |

## Замечание по режиму по умолчанию

В режиме `FLASHHUNTER_ENV=dev` (и в docker compose из коробки) сервисы общаются
внутри одного Python-процесса, и proto используется как документация контрактов
и для кодогенерации в CI. Кластерный режим (`FLASHHUNTER_ENV=prod` +
`docker compose --profile cluster up`) поднимает Rust-сервисы, которые говорят
по этим же контрактам через gRPC.
