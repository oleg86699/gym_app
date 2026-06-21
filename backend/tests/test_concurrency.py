"""Тест распределённого лимитера конкуренции (Redis ZSET) — глобальный потолок
одновременных операций (постинг)."""

from __future__ import annotations

import asyncio

from infrastructure.concurrency import RedisConcurrencyLimiter


def _mk(name: str, **kw) -> RedisConcurrencyLimiter:
    return RedisConcurrencyLimiter(
        name, key_prefix="test:climit:",
        acquire_timeout_s=kw.pop("acquire_timeout_s", 0.8),
        poll_interval_s=0.05,
        **kw,
    )


async def _clear(lim: RedisConcurrencyLimiter) -> None:
    r = await lim._get_redis()
    if r is not None:
        await r.delete(lim.key)


async def test_enforces_global_cap():
    lim = _mk("cap", stale_ttl_s=30)
    await _clear(lim)
    try:
        t1 = await lim.acquire(limit=2)
        t2 = await lim.acquire(limit=2)
        assert t1 and t2
        assert await lim.in_use() == 2
        # третий — потолок занят → ждёт acquire_timeout и отдаёт None (degrade)
        t3 = await lim.acquire(limit=2)
        assert t3 is None
        assert await lim.in_use() == 2
        # освободили слот → следующий проходит
        await lim.release(t1)
        t4 = await lim.acquire(limit=2)
        assert t4 is not None
        assert await lim.in_use() == 2
    finally:
        await _clear(lim)
        await lim.aclose()


async def test_crash_safe_stale_cleanup():
    """Если держатель «упал» и не сделал release — слот сам освободится
    после stale_ttl (ZREMRANGEBYSCORE в Lua)."""
    lim = _mk("stale", stale_ttl_s=1)
    await _clear(lim)
    try:
        await lim.acquire(limit=1)          # держим слот и НЕ релизим (симуляция краша)
        assert await lim.in_use() == 1
        # сразу новый не пройдёт
        assert await lim.acquire(limit=1) is None
        await asyncio.sleep(1.2)            # протух
        assert await lim.in_use() == 0
        assert await lim.acquire(limit=1) is not None
    finally:
        await _clear(lim)
        await lim.aclose()


async def test_slot_context_manager_releases():
    lim = _mk("slot", stale_ttl_s=30)
    await _clear(lim)
    try:
        async with lim.slot(limit=1) as tok:
            assert tok is not None
            assert await lim.in_use() == 1
        # вышли из блока → слот освобождён
        assert await lim.in_use() == 0
    finally:
        await _clear(lim)
        await lim.aclose()
