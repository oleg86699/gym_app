"""publish_run_event — best-effort: сбой Redis НИКОГДА не валит постинг.

Регрессия: в except-хендлере стоял `event=event`, что коллизит с reserved
first-arg structlog → сам логгер бросал TypeError, который пробивал наружу и
ронял posting-таск (run застревал в RUNNING, ничего не постилось).
"""

from __future__ import annotations

import core.realtime as rt


class _BoomClient:
    async def publish(self, *a, **k):
        raise RuntimeError("Event loop is closed")


async def test_publish_run_event_swallows_redis_failure(monkeypatch):
    monkeypatch.setattr(rt, "_get_async_client", lambda: _BoomClient())
    # не должно бросить (раньше падало TypeError из-за event=-коллизии)
    await rt.publish_run_event(999_001, "status", {"status": "running"})


def test_publish_run_event_sync_swallows_redis_failure(monkeypatch):
    class _SyncBoom:
        def publish(self, *a, **k):
            raise RuntimeError("nope")
    monkeypatch.setattr(rt, "_get_sync_client", lambda: _SyncBoom())
    rt.publish_run_event_sync(999_002, "status", {"status": "running"})


async def test_async_client_rebinds_on_loop_change(monkeypatch):
    # симулируем смену running-loop → клиент должен пересоздаться (иначе
    # 'Event loop is closed' в Celery, где каждый таск — свой asyncio.run()).
    created = {"n": 0}
    monkeypatch.setattr(rt, "_async_client", None)
    monkeypatch.setattr(rt, "_async_loop", object())  # «другой» loop
    monkeypatch.setattr(rt.aioredis, "from_url",
                        lambda *a, **k: created.__setitem__("n", created["n"] + 1) or object())
    rt._get_async_client()
    assert created["n"] == 1  # пересоздан под текущий loop
