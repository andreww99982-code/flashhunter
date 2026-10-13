"""Реальный JSON-RPC клиент: HTTP с батчингом и WebSocket-подписки.

Здесь нет ни одного выдуманного значения: клиент ходит в настоящие узлы
Ethereum (publicnode / Alchemy / QuickNode — что задано в .env), батчит вызовы
и держит статистику задержек по каждому методу.

Ключевые решения:
  * ``User-Agent`` обязателен: публичные узлы отдают 403 на пустой UA;
  * батчи (JSON-RPC array) вместо сотни отдельных запросов — иначе мы не уложимся
    в бюджет блока и упрёмся в rate-limit публичного провайдера;
  * failover: несколько эндпоинтов, при ошибке запрос уходит на следующий;
  * WebSocket отдельно от HTTP: ``newHeads`` даёт блок на десятки миллисекунд
    раньше, чем поллинг, а ``newPendingTransactions`` — доступ к реальному мемпулу.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import random
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Публичные узлы режут запросы без внятного User-Agent
# Порог серии ошибок, после которого узел уходит в карантин, и границы паузы
HEALTH_FAIL_THRESHOLD = 3
HEALTH_BASE_COOLDOWN = 5.0
HEALTH_MAX_COOLDOWN = 60.0

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "FlashHunter/0.1 (+https://github.com/flashhunter/flashhunter)",
    "Accept": "application/json",
}


class RpcError(RuntimeError):
    """Ошибка узла (JSON-RPC error) с сохранением кода и сообщения."""

    def __init__(self, code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload


class RpcBatchTooLarge(RuntimeError):
    """Узел отказал из-за размера батча и сам назвал допустимый предел.

    Это политика тарифа («Batch of more than 3 requests are not allowed on free plan»),
    а не сбой: повторять запрос бессмысленно — нужно уменьшить размер и идти дальше.
    """

    def __init__(self, message: str, limit: int = 0) -> None:
        super().__init__(message)
        self.limit = limit


class RpcUnavailable(RuntimeError):
    """Все эндпоинты недоступны либо превышено число попыток."""


BATCH_LIMIT_PATTERN = re.compile(r"more than (\d+) request", re.IGNORECASE)


def parse_batch_limit(text: str) -> int:
    """Достаёт из ответа узла допустимый размер батча («more than 3 requests»).

    Без этого приходится подбирать размер половинным делением, теряя на каждую
    попытку сотни миллисекунд — в MEV это потерянный блок.
    """
    match = BATCH_LIMIT_PATTERN.search(text or "")
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


@dataclass(slots=True)
class RpcStats:
    """Статистика по эндпоинту: сколько вызовов, ошибок и средняя задержка."""

    url: str
    calls: int = 0
    errors: int = 0
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        """Средняя задержка вызова, мс."""
        return self.total_ms / self.calls if self.calls else 0.0


@dataclass(slots=True)
class RpcResult:
    """Результат одного вызова внутри батча."""

    id: int
    ok: bool
    value: Any = None
    error: str = ""


class AsyncRpcPool:
    """Пул HTTP JSON-RPC эндпоинтов с батчингом, ретраями и failover.

    Пример::

        rpc = AsyncRpcPool(["https://ethereum-rpc.publicnode.com"])
        head = await rpc.block_number()
        data = await rpc.batch([("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), ...])
    """

    def __init__(
        self,
        urls: Sequence[str],
        *,
        timeout: float = 20.0,
        retries: int = 3,
        max_batch: int = 30,
        max_concurrent: int = 6,
        min_interval: float = 0.05,
        headers: dict[str, str] | None = None,
    ) -> None:
        candidates = [url.strip() for url in urls if url and url.strip()]
        if not candidates:
            raise ValueError("не задан ни один RPC-эндпоинт (проверьте ETH_RPC_HTTP и т.п.)")
        self.urls = candidates
        self.timeout = timeout
        self.retries = retries
        self.max_batch = max_batch
        # Публичные RPC режут частые параллельные запросы (403), поэтому:
        #   * не больше max_concurrent одновременных POST на пул;
        #   * пауза min_interval между запросами к одному эндпоинту.
        self._sema = asyncio.Semaphore(max_concurrent)
        self._min_interval = min_interval
        self._last_post_at: dict[str, float] = {}
        self.headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._client: httpx.AsyncClient | None = None
        self._ids = itertools.count(1)
        self.stats: dict[str, RpcStats] = {url: RpcStats(url) for url in candidates}
        self._rr = 0
        # Здоровье эндпоинтов: публичные узлы периодически начинают отдавать 4xx/5xx
        # целыми сериями. Держать их в ротации — значит платить латентностью за
        # заведомо неудачные попытки. Считаем серию ошибок и уводим узел в «карантин»
        # на возрастающее время, пока он не восстановится.
        self._fail_streak: dict[str, int] = {url: 0 for url in candidates}
        # Предел батча по каждому узлу: публичные RPC отличаются в разы
        self._batch_limit: dict[str, int] = {url: max_batch for url in candidates}
        self._cooldown_until: dict[str, float] = {url: 0.0 for url in candidates}
        self._cooldown_notice: dict[str, int] = {url: 0 for url in candidates}

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> AsyncRpcPool:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Создаёт HTTP-клиент (переиспользование соединений — критично для латентности)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                http2=False,
            )

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # Здоровье эндпоинтов
    # ------------------------------------------------------------------ #
    def _on_success(self, url: str) -> None:
        """Успешный запрос: серия ошибок сброшена, узел здоров."""
        self._fail_streak[url] = 0
        self._cooldown_until[url] = 0.0
        self._cooldown_notice[url] = 0

    def _on_failure(self, url: str, exc: Exception) -> None:
        """Ошибка запроса: после серии неудач узел уходит в карантин с ростом паузы."""
        streak = self._fail_streak.get(url, 0) + 1
        self._fail_streak[url] = streak
        if streak < HEALTH_FAIL_THRESHOLD:
            return
        # 5 с, 10 с, 20 с, 40 с, максимум 60 с — узел сам сообщит о готовности
        penalty = min(HEALTH_MAX_COOLDOWN, HEALTH_BASE_COOLDOWN * (2 ** (streak - HEALTH_FAIL_THRESHOLD)))
        self._cooldown_until[url] = time.time() + penalty
        if self._cooldown_notice.get(url, 0) != streak:
            self._cooldown_notice[url] = streak
            log.warning("RPC %s отключён на %.0f с после %d ошибок подряд (%s)", url, penalty, streak, exc)

    def healthy_endpoints(self) -> list[str]:
        """Список эндпоинтов, доступных прямо сейчас (для отчётов и health-эндпоинта)."""
        now = time.time()
        return [url for url in self.urls if self._cooldown_until.get(url, 0.0) <= now]

    def _endpoints(self) -> list[str]:
        """Порядок эндпоинтов: сначала здоровые, затем карантинные — как резерв.

        Карантин не означает «никогда»: если все узлы отвалились, очередь отдаст
        именно их, и запрос всё равно будет предпринят.
        """
        now = time.time()
        self._rr = (self._rr + 1) % len(self.urls)
        rotated = self.urls[self._rr:] + self.urls[: self._rr]
        healthy = [url for url in rotated if self._cooldown_until.get(url, 0.0) <= now]
        quarantined = [url for url in rotated if self._cooldown_until.get(url, 0.0) > now]
        return healthy + quarantined

    async def _post(self, url: str, payload: Any) -> Any:
        """Один HTTP-запрос с ретраями и учётом статистики."""
        if self._client is None:
            await self.start()
        assert self._client is not None
        stats = self.stats[url]
        last_error: Exception | None = None

        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                # Держим минимальный интервал между запросами к одному эндпоинту
                elapsed = time.time() - self._last_post_at.get(url, 0.0)
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)
                async with self._sema:
                    self._last_post_at[url] = time.time()
                    response = await self._client.post(url, json=payload)
                elapsed_ms = (time.perf_counter() - started) * 1000
                stats.calls += 1
                stats.total_ms += elapsed_ms
                self._on_success(url)

                if response.status_code == 429 or response.status_code >= 500:
                    limit = parse_batch_limit(response.text)
                    if limit:
                        # Тарифный предел: повторять тот же размер нет смысла
                        raise RpcBatchTooLarge(f"HTTP {response.status_code} от {url}: {response.text[:120]}", limit)
                    raise RpcUnavailable(f"HTTP {response.status_code} от {url}: {response.text[:120]}")
                if response.status_code == 403:
                    # Публичные узлы так отвечают на «серые» User-Agent/лимиты
                    raise RpcUnavailable(f"HTTP 403 от {url}: {response.text[:120]}")
                response.raise_for_status()
                return response.json()
            except RpcBatchTooLarge:
                # Не ретраим: лимит тарифа не изменится от повторов
                stats.errors += 1
                raise
            except Exception as exc:  # noqa: BLE001 — нужен единый путь ретраев
                last_error = exc
                stats.errors += 1
                self._on_failure(url, exc)
                # Экспоненциальная пауза с джиттером: не долбим узел, но и не ждём секунды
                delay = min(0.15 * (2**attempt), 2.0) * (0.7 + random.random() * 0.6)
                log.debug("RPC %s попытка %d/%d: %s (пауза %.2f с)", url, attempt + 1, self.retries, exc, delay)
                if attempt + 1 < self.retries:
                    await asyncio.sleep(delay)

        raise RpcUnavailable(f"эндпоинт {url} недоступен после {self.retries} попыток: {last_error}")

    # ------------------------------------------------------------------ #
    async def batch(self, calls: Sequence[tuple[str, list[Any]]]) -> list[RpcResult]:
        """Батч вызовов: результаты идут строго в порядке вызовов.

        Публичные узлы ведут себя по-разному: одни принимают большие батчи, другие
        отвечают одним объектом с ошибкой «too many RPC calls in batch request».
        Раньше такой ответ приводил к тому, что весь чанк молча считался «пустым»
        (данные пулов пропадали из снимка). Теперь:
          * отказ всего чанка — это повод разбить его и доспросить, а не потерять данные;
          * лимит на размер батча ведётся по каждому узлу отдельно и уменьшается,
            если узел на размер жалуется;
          * узел, который не умеет батчи, используется только для одиночных вызовов.
        """
        results: list[RpcResult] = []
        for offset in range(0, len(calls), self.max_batch):
            chunk = list(calls[offset : offset + self.max_batch])
            results.extend(await self._batch_chunk(chunk))
        return results

    async def _batch_chunk(self, chunk: list[tuple[str, list[Any]]]) -> list[RpcResult]:
        """Один чанк батча: перебор узлов, разбиение при отказе, добор недостающих."""
        payload = [
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            for method, params in chunk
        ]
        request_ids = [int(item["id"]) for item in payload]
        last_error: Exception | None = None

        for url in self._endpoints():
            limit = self._batch_limit.get(url, self.max_batch)
            if len(chunk) > limit and self._has_batch_capable_alternative(url):
                # Узел уже показал, что такой размер ему не по силам — не тратим на него время
                continue
            try:
                body = await self._post(url, payload)
            except RpcBatchTooLarge as exc:
                # Узел сам назвал предел: запоминаем точное значение и идём к другому узлу
                self._set_batch_limit(url, exc.limit, str(exc)[:120])
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                if "too many" in str(exc).lower() or "batch" in str(exc).lower():
                    self._shrink_batch_limit(url, str(exc)[:120])
                last_error = exc
                continue

            parsed, single_error = self._parse_batch(body)
            if parsed is None:
                # Узел отверг чанк целиком одним объектом ошибки (или отдал мусор)
                self._shrink_batch_limit(url, single_error)
                last_error = RpcUnavailable(f"{url}: {single_error}")
                continue

            missing = [request_id for request_id in request_ids if request_id not in parsed]
            if missing and len(chunk) > 1:
                # Часть ответов не пришла: добираем оставшееся половинками, а не теряем
                log.debug("батч: %d из %d ответов от %s, доразбиваем", len(missing), len(chunk), url)
                half = len(chunk) // 2
                return await self._batch_chunk(chunk[:half]) + await self._batch_chunk(chunk[half:])

            return [
                parsed.get(
                    request_id,
                    RpcResult(request_id, False, error=f"нет ответа от {url}"),
                )
                for request_id in request_ids
            ]

        # Ни один узел не взял чанк целиком: делим его и добираем по частям
        if len(chunk) > 1:
            half = len(chunk) // 2
            log.debug("батч из %d вызовов разбит из-за отказа узлов: %s", len(chunk), last_error)
            return await self._batch_chunk(chunk[:half]) + await self._batch_chunk(chunk[half:])
        raise RpcUnavailable(f"батч из 1 вызова не выполнен: {last_error}")

    def _parse_batch(self, body: Any) -> tuple[dict[int, RpcResult] | None, str]:
        """Разбирает ответ батча. ``None`` в первом элементе — узел отверг весь чанк.

        Возвращает (ответы по id, текст ошибки). Одиночный объект с ошибкой (идёт
        как ответ на весь батч) распознаётся именно здесь.
        """
        if isinstance(body, dict):
            if "error" in body and "result" not in body:
                return None, str(body.get("error", ""))[:160]
            body = [body]
        if not isinstance(body, list) or not body:
            return None, f"неожиданный ответ узла: {str(body)[:120]}"

        by_id: dict[int, RpcResult] = {}
        for item in body:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if raw_id is None:
                continue
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if "error" in item:
                error = item["error"]
                by_id[item_id] = RpcResult(item_id, False, error=str(error.get("message", "error")))
            else:
                by_id[item_id] = RpcResult(item_id, True, value=item.get("result"))
        return by_id, ""

    def _set_batch_limit(self, url: str, limit: int, reason: str) -> None:
        """Устанавливает предел батча по значению, которое назвал сам узел."""
        limit = max(1, int(limit))
        current = self._batch_limit.get(url, self.max_batch)
        if limit >= current:
            return
        self._batch_limit[url] = limit
        log.warning("RPC %s: предел батча установлен в %d по ответу узла (%s)", url, limit, reason[:80])

    def _shrink_batch_limit(self, url: str, reason: str) -> None:
        """Уменьшает предел батча для узла, который жалуется на размер запроса."""
        current = self._batch_limit.get(url, self.max_batch)
        if current <= 1:
            return
        new_limit = max(1, current // 2)
        self._batch_limit[url] = new_limit
        log.warning("RPC %s: предел батча снижен до %d (%s)", url, new_limit, reason[:80])

    def _has_batch_capable_alternative(self, url: str) -> bool:
        """Есть ли другой узел, который способен принять батч текущего размера."""
        return any(
            other != url and self._cooldown_until.get(other, 0.0) <= time.time() and self._batch_limit.get(other, self.max_batch) > 1
            for other in self.urls
        )

    async def call(self, method: str, params: list[Any]) -> Any:
        """Одиночный вызов с перебором эндпоинтов."""
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        last_error: Exception | None = None

        for url in self._endpoints():
            try:
                body = await self._post(url, payload)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            if "error" in body:
                error = body["error"]
                raise RpcError(int(error.get("code", -32000)), str(error.get("message", "unknown")), error.get("data"))
            return body.get("result")

        raise RpcUnavailable(f"{method}: все эндпоинты недоступны ({last_error})")

    async def block_number(self) -> int:
        """Номер последнего блока."""
        return int(await self.call("eth_blockNumber", []), 16)

    async def get_block(self, number: int | str = "latest", *, full_txs: bool = False) -> dict[str, Any]:
        """Заголовок блока (или блок целиком, если ``full_txs=True``)."""
        tag = hex(number) if isinstance(number, int) else number
        block = await self.call("eth_getBlockByNumber", [tag, full_txs])
        if not block:
            raise RpcUnavailable(f"блок {tag} не найден (возможно, ещё не добыт)")
        return block

    async def get_logs(
        self,
        *,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
        from_block: int | str,
        to_block: int | str = "latest",
    ) -> list[dict[str, Any]]:
        """Логи по фильтру (реальный ``eth_getLogs``)."""
        params: dict[str, Any] = {
            "fromBlock": hex(from_block) if isinstance(from_block, int) else from_block,
            "toBlock": hex(to_block) if isinstance(to_block, int) else to_block,
        }
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        return list(await self.call("eth_getLogs", [params]) or [])

    async def eth_call(self, to: str, data: str, *, block: int | str = "latest") -> str:
        """Одиночный ``eth_call`` — возвращает hex-результат."""
        tag = hex(block) if isinstance(block, int) else block
        return str(await self.call("eth_call", [{"to": to, "data": data}, tag]))

    async def fee_history(self, blocks: int = 5, percentiles: Sequence[int] = (25, 50, 75)) -> dict[str, Any]:
        """История комиссий: base fee и перцентили чаевых (реальные данные)."""
        head = await self.block_number()
        return dict(await self.call("eth_feeHistory", [hex(blocks), hex(head), list(percentiles)]))

    async def max_priority_fee(self) -> int:
        """Рекомендуемые чаевые от узла, wei."""
        try:
            return int(await self.call("eth_maxPriorityFeePerGas", []), 16)
        except (RpcError, RpcUnavailable):
            # Узел может не поддерживать метод — считаем по истории
            history = await self.fee_history(3)
            rewards = [int(row[1], 16) for row in history.get("reward", []) if row]
            return int(sum(rewards) / len(rewards)) if rewards else 0

    async def get_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        """Транзакция по хэшу (None — неизвестна узлу)."""
        result = await self.call("eth_getTransactionByHash", [tx_hash])
        return dict(result) if result else None

    async def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """Квитанция транзакции (None — ещё не добыта)."""
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        return dict(result) if result else None

    async def get_transaction_count(self, address: str, *, pending: bool = True) -> int:
        """Nonce аккаунта: ``pending`` учитывает уже отправленные транзакции."""
        tag = "pending" if pending else "latest"
        return int(await self.call("eth_getTransactionCount", [address, tag]), 16)

    async def get_balance(self, address: str) -> int:
        """Баланс в wei на последнем блоке."""
        return int(await self.call("eth_getBalance", [address, "latest"]), 16)

    async def estimate_gas(self, tx: dict[str, Any], *, block: int | str = "latest") -> int:
        """Реальная оценка газа для конкретного вызова."""
        tag = hex(block) if isinstance(block, int) else block
        return int(await self.call("eth_estimateGas", [tx, tag]), 16)

    async def gas_price(self) -> int:
        """Текущая цена газа, wei (``eth_gasPrice``)."""
        return int(await self.call("eth_gasPrice", []), 16)

    async def send_raw_transaction(self, raw_hex: str) -> str:
        """Отправка подписанной транзакции в сеть (реальный мемпул)."""
        return str(await self.call("eth_sendRawTransaction", [raw_hex]))

    # ------------------------------------------------------------------ #
    def snapshot(self) -> list[dict[str, Any]]:
        """Снимок состояния эндпоинтов для страницы /latency и метрик."""
        return [
            {
                "url": stats.url,
                "calls": stats.calls,
                "errors": stats.errors,
                "avg_ms": round(stats.avg_ms, 2),
                "healthy": stats.calls == 0 or stats.errors / max(stats.calls, 1) < 0.2,
            }
            for stats in self.stats.values()
        ]


# --------------------------------------------------------------------------- #
# WebSocket: заголовки блоков и реальный мемпул
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WsEvent:
    """Событие подписки: новый заголовок блока или хэш транзакции из мемпула."""

    kind: str
    payload: Any
    received_at: float = field(default_factory=time.time)


class WsFeed:
    """Подписка на ``newHeads`` и ``newPendingTransactions`` по WebSocket.

    Зачем WebSocket, а не поллинг: заголовок блока приходит на 50–300 мс раньше,
    а список ожидающих транзакций вообще недоступен по HTTP в публичных узлах.
    Оба потока критичны: голова блока запускает пайплайн, мемпул даёт backrun/JIT.
    """

    def __init__(
        self,
        url: str,
        *,
        subscriptions: Sequence[str] = ("newHeads",),
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 20.0,
    ) -> None:
        self.url = url
        self.subscriptions = list(subscriptions)
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.queue: asyncio.Queue[WsEvent] = asyncio.Queue(maxsize=4096)
        self.connected: bool = False
        self.last_error: str = ""
        self.dropped_events: int = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Запускает фоновую задачу чтения подписок."""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name=f"ws-feed:{self.url}")

    async def stop(self) -> None:
        """Останавливает чтение и закрывает соединение."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.connected = False

    async def _run(self) -> None:
        """Цикл подключения с экспоненциальным переподключением."""
        import websockets

        delay = self.reconnect_delay
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, close_timeout=5, max_queue=4096) as ws:
                    self.connected = True
                    self.last_error = ""
                    delay = self.reconnect_delay
                    log.info("WS подключён: %s", self.url)

                    request_id = 0
                    for subscription in self.subscriptions:
                        request_id += 1
                        await ws.send(
                            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "eth_subscribe", "params": [subscription]})
                        )
                        await ws.recv()  # подтверждение подписки

                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сеть рвётся штатно, переподключаемся
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("WS %s отключился (%s), переподключение через %.1f с", self.url, self.last_error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_delay)

    def _handle(self, raw: str | bytes) -> None:
        """Разбирает сообщение подписки и кладёт событие в очередь."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        result = params.get("result")
        if isinstance(result, dict):  # newHeads: полный заголовок
            event = WsEvent("head", result)
        elif isinstance(result, str):  # newPendingTransactions: хэш транзакции
            event = WsEvent("pending", result)
        else:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Очередь переполнена: событие теряем, но счётчик покажет масштаб проблемы
            self.dropped_events += 1

    async def next_event(self, timeout: float = 1.0) -> WsEvent | None:
        """Забирает следующее событие (None по таймауту)."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def snapshot(self) -> dict[str, Any]:
        """Состояние фида для метрик и страницы /latency."""
        return {
            "url": self.url,
            "connected": self.connected,
            "queue": self.queue.qsize(),
            "dropped": self.dropped_events,
            "last_error": self.last_error,
            "subscriptions": self.subscriptions,
        }
