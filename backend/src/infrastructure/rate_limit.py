"""Распределённый per-domain rate limiter (Redis).

ЗАЧЕМ: in-process limiter защищает только корутины ОДНОГО процесса. На проде
работают параллельно несколько worker-процессов (Celery posting + TaskIQ
validation + N реплик). Каждый со своим in-memory limiter → один домен
получает N×больше запросов чем задумано → попадаем под rate-limit/anti-bot
плагины WP, портим прокси, рушим валидность.

РЕШЕНИЕ: общий лимит через Redis. `SET key 1 NX PX interval` атомарно
гарантирует «не чаще 1 раза в interval на домен» через ВСЕ процессы.

Fallback: если Redis недоступен — degrade на локальный sleep (лучше чем
ничего; не блокируем работу).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import structlog

log = structlog.get_logger(__name__)


class RedisDomainRateLimiter:
    """Не чаще 1 запроса в `min_interval` секунд на домен — across processes.

    acquire(domain) блокирует (await) пока не наступит окно. Реализация:
    атомарный `SET ratelimit:dom:<d> 1 NX PX <interval_ms>`:
      - OK    → ключ установлен, окно свободно, идём дальше
      - nil   → кто-то уже занял окно; ждём PTTL и пробуем снова
    """

    def __init__(self, min_interval_s: float, *, key_prefix: str = "ratelimit:dom:"):
        self.min_interval = min_interval_s
        self.key_prefix = key_prefix
        self._redis = None
        self._redis_failed = False
        # Локальный fallback (если Redis лёг)
        self._local_last: dict[str, float] = defaultdict(float)
        self._local_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _get_redis(self):
        if self._redis is not None or self._redis_failed:
            return self._redis
        try:
            import redis.asyncio as aioredis

            from core.config import settings

            self._redis = aioredis.from_url(settings.REDIS_URL)
            await self._redis.ping()
        except Exception as e:
            log.warning("rate_limit.redis_unavailable", error=str(e))
            self._redis_failed = True
            self._redis = None
        return self._redis

    async def acquire(self, domain: str) -> None:
        r = await self._get_redis()
        if r is None:
            await self._acquire_local(domain)
            return

        key = f"{self.key_prefix}{domain}"
        interval_ms = int(self.min_interval * 1000)
        # Защита от бесконечного цикла (домен под жёстким контеншеном):
        # максимум ждём ~2×interval суммарно.
        deadline = time.monotonic() + self.min_interval * 2 + 1
        while True:
            try:
                ok = await r.set(key, 1, nx=True, px=interval_ms)
            except Exception as e:
                log.warning("rate_limit.redis_set_failed", error=str(e))
                await self._acquire_local(domain)
                return
            if ok:
                return
            # Окно занято — ждём остаток TTL
            try:
                ttl = await r.pttl(key)
            except Exception:
                ttl = interval_ms
            wait_s = max(0.05, (ttl or interval_ms) / 1000)
            if time.monotonic() + wait_s > deadline:
                # Слишком долго — пропускаем (не вечно блокируем воркер)
                log.debug("rate_limit.giveup", domain=domain)
                return
            await asyncio.sleep(wait_s)

    async def _acquire_local(self, domain: str) -> None:
        """In-process fallback (Redis недоступен)."""
        async with self._local_locks[domain]:
            now = time.monotonic()
            elapsed = now - self._local_last[domain]
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._local_last[domain] = time.monotonic()

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
