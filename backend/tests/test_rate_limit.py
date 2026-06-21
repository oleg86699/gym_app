"""Тест распределённого per-domain rate limiter-а (Redis)."""

from __future__ import annotations

import time

from infrastructure.rate_limit import RedisDomainRateLimiter


async def test_enforces_interval_per_domain():
    rl = RedisDomainRateLimiter(min_interval_s=0.5, key_prefix="test:rl:enforce:")
    try:
        t0 = time.monotonic()
        await rl.acquire("a.example")        # свободно
        t1 = time.monotonic()
        await rl.acquire("a.example")        # тот же домен — ждёт окно
        t2 = time.monotonic()
        await rl.acquire("b.example")        # другой домен — сразу
        t3 = time.monotonic()
    finally:
        await rl.aclose()

    assert t2 - t1 >= 0.4, "повторный вызов того же домена должен ждать интервал"
    assert t3 - t2 < 0.3, "другой домен не должен ждать"


async def test_independent_domains_parallel():
    """Разные домены не блокируют друг друга при параллельных acquire."""
    import asyncio

    rl = RedisDomainRateLimiter(min_interval_s=0.5, key_prefix="test:rl:par:")
    try:
        t0 = time.monotonic()
        await asyncio.gather(*[rl.acquire(f"d{i}.example") for i in range(5)])
        elapsed = time.monotonic() - t0
    finally:
        await rl.aclose()
    # 5 разных доменов параллельно — должно быть быстро (не 5×интервал)
    assert elapsed < 0.5
