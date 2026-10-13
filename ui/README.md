# UI: панель управления FlashHunter

React 18 + TypeScript + Vite 5 + Tailwind. Material Design 3 с эффектом
Liquid Glass: 12 страниц, тёмная тема, живое обновление по WebSocket.

## Токены дизайна

| Токен | Значение | Где используется |
|-------|----------|------------------|
| `--bg-1` | `#0a0e27` | Основной фон (глубокий синий) |
| `--accent-profit` | `#00ffa3` | Прибыль, положительные дельты |
| `--accent-loss` | `#ff4d6d` | Убытки, алерты, ошибки |
| `--accent-primary` | `#7c5cff` | Акценты, ссылки, активные элементы |
| Стекло | blur 24px | Карточки, панели, модальные окна |

## Страницы

`/login` · `/dashboard` · `/opportunities` · `/strategies` · `/contracts` ·
`/wallets` · `/history` · `/analytics` · `/latency` · `/settings` · `/logs` · `/backtest`

## Разработка

```bash
npm install
npm run dev        # http://localhost:5173, проксирует /api и /ws на :8000
npx tsc --noEmit   # проверка типов
npm run build      # сборка в python/flashhunter/ui_static (отдаёт FastAPI)
```

Важно: `outDir` в `vite.config.ts` указывает на `../python/flashhunter/ui_static`,
поэтому собранная панель попадает внутрь Python-пакета и раздаётся тем же
процессом FastAPI — один порт, без CORS.

## Взаимодействие с API

* Авторизация: JWT хранится в `localStorage` под ключом `fh_token`,
  cookie `fh_session` выставляется сервером.
* Запросы: обёртка в `src/lib/api.ts` (единая обработка 401 → редирект на `/login`).
* Живые данные: WebSocket `/ws?token=<JWT>` — в `src/lib/ws.ts`, статус соединения
  виден в шапке панели.

## Сборка в Docker

`infra/docker/Dockerfile.api` собирает панель отдельной ступенью (node:20-alpine)
и копирует результат в образ Python — прод-образ самодостаточен.
