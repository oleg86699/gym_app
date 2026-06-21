"""
Async Redis pub/sub для SSE-канала прогресса прогонов.

Канал per-run: `run:{run_id}`.

- Worker (Celery, sync) → `publish_run_event` через sync-helper.
- FastAPI (async) → `subscribe_run_events` async generator.

Используется отдельный logical Redis DB чтобы не пересекаться с Celery/TaskIQ
очередями. Если PUBSUB_REDIS_URL не задан в env, используется `REDIS_URL`
с базой `/8`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import redis
import redis.asyncio as aioredis
import structlog

from core.config import settings

log = structlog.get_logger(__name__)


def _pubsub_url() -> str:
    base = settings.REDIS_URL.rsplit("/", 1)[0]
    return f"{base}/8"


def run_channel(run_id: int) -> str:
    return f"run:{run_id}"


# ─── Sync publish (для Celery worker внутри asyncio.to_thread) ───────


_sync_client: redis.Redis | None = None


def _get_sync_client() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.from_url(_pubsub_url(), decode_responses=True)
    return _sync_client


def publish_run_event_sync(run_id: int, event: str, data: dict[str, Any]) -> None:
    """Sync вариант publish для использования внутри Celery loop через to_thread."""
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
    try:
        _get_sync_client().publish(run_channel(run_id), payload)
    except Exception as e:
        # Не критично — UI обновится при следующем событии или fallback на refresh.
        # `evt=` (не `event=`) — иначе коллизия с reserved-аргументом structlog.
        log.warning("realtime.publish.failed", run_id=run_id, evt=event, error=str(e))


# ─── Async publish (для FastAPI / TaskIQ) ────────────────────────────


_async_client: aioredis.Redis | None = None
_async_loop: object | None = None


def _get_async_client() -> aioredis.Redis:
    """Async Redis-клиент, привязанный к ТЕКУЩЕМУ event loop.

    В Celery каждая задача крутится в своём `asyncio.run()` (новый loop), а пул
    aioredis кэширует loop создания — переиспользование в новом loop даёт
    `RuntimeError: Event loop is closed`. Поэтому пересоздаём клиент при смене
    running-loop.
    """
    global _async_client, _async_loop
    try:
        loop: object | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _async_client is None or _async_loop is not loop:
        _async_client = aioredis.from_url(_pubsub_url(), decode_responses=True)
        _async_loop = loop
    return _async_client


async def publish_run_event(run_id: int, event: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
    try:
        await _get_async_client().publish(run_channel(run_id), payload)
    except Exception as e:
        # best-effort: потерять realtime-событие не страшно (UI поллит fallback-ом),
        # но НИКОГДА не валим постинг. Сбрасываем клиент → пересоздастся на текущем
        # loop. `evt=` (не `event=`) — иначе коллизия с reserved-аргументом structlog
        # и сам логгер бросит TypeError.
        global _async_client, _async_loop
        _async_client = None
        _async_loop = None
        log.warning("realtime.publish.failed", run_id=run_id, evt=event, error=str(e))


# ─── Async subscribe (для SSE endpoint) ──────────────────────────────


async def subscribe_run_events(run_id: int) -> AsyncIterator[str]:
    """
    Подписаться на канал, отдавать payload-ы (JSON-строки) пока не закроется
    consumer. Closing/cancel из FastAPI закрывает pubsub автоматически в finally.
    """
    pubsub = _get_async_client().pubsub()
    channel = run_channel(run_id)
    try:
        await pubsub.subscribe(channel)
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                if data:
                    yield data
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass
