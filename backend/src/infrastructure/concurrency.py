"""Распределённый лимитер конкуренции (Redis) — глобальный потолок на число
одновременных операций через ВСЕ процессы и task-и.

ЗАЧЕМ: каждый posting-run — отдельный Celery-task со своим in-process
`asyncio.Semaphore(run.concurrency)`. При 2 celery-процессах и N активных
run-ах суммарная нагрузка = Σ run.concurrency — никто не ограничивает аппетит
на дефицитный общий ресурс (пул прокси, PgBouncer, исходящий трафик). Один
пользователь с 5 прогонами мог бы выжрать всю ёмкость.

РЕШЕНИЕ: общий «семафор» в Redis. Все run-task-и (в любом процессе) делят один
потолок `limit`. Items разных run-ов чередуются на нём → «всё двигается
понемногу», без жёсткого приоритета и без динамического governor-а.

РЕАЛИЗАЦИЯ: ZSET `climit:<name>` = держатели слотов (member=token, score=ts).
Acquire/cleanup/check — одним Lua (атомарно под контеншеном):
  1. ZREMRANGEBYSCORE  — выкинуть протухших держателей (старше stale_ttl):
     CRASH-SAFE — если воркер упал и не сделал release, слот сам освободится.
  2. ZCARD < limit ?   — есть место → ZADD token, EXPIRE ключа; вернуть 1.
  3. иначе             — вернуть 0 (caller подождёт и повторит).

Fallback: Redis недоступен → degrade на in-process semaphore (лучше, чем
вообще без лимита; не блокируем работу).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog

log = structlog.get_logger(__name__)


# Atomic acquire: cleanup stale → check cap → add token. KEYS[1]=zset,
# ARGV: now, limit, stale_ttl, token. Returns 1 (got slot) | 0 (full).
_ACQUIRE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, token)
  redis.call('EXPIRE', key, math.ceil(ttl * 2))
  return 1
end
return 0
"""


class RedisConcurrencyLimiter:
    """Глобальный потолок `limit` одновременных слотов across processes.

    Использование:
        limiter = RedisConcurrencyLimiter("posting", stale_ttl_s=180)
        async with limiter.slot(limit=80):
            ...  # дефицитная операция (один пост)
    """

    def __init__(
        self,
        name: str,
        *,
        stale_ttl_s: float = 180.0,
        key_prefix: str = "climit:",
        acquire_timeout_s: float = 120.0,
        poll_interval_s: float = 0.25,
    ):
        self.name = name
        self.key = f"{key_prefix}{name}"
        self.stale_ttl = stale_ttl_s
        self.acquire_timeout = acquire_timeout_s
        self.poll_interval = poll_interval_s
        self._redis = None
        self._redis_failed = False
        # in-process fallback semaphore (если Redis лёг) — лимит по последнему
        # известному значению; создаётся лениво в _acquire_local.
        self._local_sem: asyncio.Semaphore | None = None
        self._local_limit: int | None = None

    async def _get_redis(self):
        if self._redis is not None or self._redis_failed:
            return self._redis
        try:
            import redis.asyncio as aioredis

            from core.config import settings

            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()
        except Exception as e:
            log.warning("concurrency.redis_unavailable", name=self.name, error=str(e))
            self._redis_failed = True
            self._redis = None
        return self._redis

    async def acquire(self, *, limit: int) -> str | None:
        """Блокирует (await) пока не освободится слот или не истечёт
        acquire_timeout. Возвращает token (для release) или None при degrade/
        таймауте — caller всё равно продолжает (лимитер — мягкая защита, не
        gate, который должен ронять работу)."""
        limit = max(1, int(limit))
        r = await self._get_redis()
        if r is None:
            return await self._acquire_local(limit)

        token = uuid.uuid4().hex
        deadline = time.monotonic() + self.acquire_timeout
        # лёгкий джиттер на старте, чтобы корутины разных run-ов не били
        # Redis синхронно в один тик (честнее делёж слотов).
        await asyncio.sleep((hash(token) % 50) / 1000.0)
        while True:
            try:
                ok = await r.eval(
                    _ACQUIRE_LUA, 1, self.key,
                    repr(time.time()), str(limit), str(self.stale_ttl), token,
                )
            except Exception as e:
                log.warning("concurrency.redis_eval_failed", name=self.name, error=str(e))
                self._redis_failed = True
                return await self._acquire_local(limit)
            if ok == 1:
                return token
            if time.monotonic() >= deadline:
                # Слишком долго ждём слот — продолжаем без него (не вешаем run).
                log.info("concurrency.acquire_timeout", name=self.name, limit=limit)
                return None
            await asyncio.sleep(self.poll_interval)

    async def release(self, token: str | None) -> None:
        if not token:
            return
        if token.startswith("local:"):
            self._release_local()
            return
        r = await self._get_redis()
        if r is None:
            return
        try:
            await r.zrem(self.key, token)
        except Exception as e:
            log.debug("concurrency.release_failed", name=self.name, error=str(e))

    async def in_use(self, *, cleanup: bool = True) -> int:
        """Сколько слотов сейчас занято (для индикатора throttled в Global
        Queue). cleanup=True сначала выкидывает протухших держателей."""
        r = await self._get_redis()
        if r is None:
            return 0
        try:
            if cleanup:
                await r.zremrangebyscore(self.key, "-inf", time.time() - self.stale_ttl)
            return int(await r.zcard(self.key) or 0)
        except Exception:
            return 0

    # ─── in-process fallback (Redis недоступен) ───────────────────────
    async def _acquire_local(self, limit: int) -> str:
        if self._local_sem is None or self._local_limit != limit:
            self._local_sem = asyncio.Semaphore(limit)
            self._local_limit = limit
        await self._local_sem.acquire()
        return "local:1"

    def _release_local(self) -> None:
        if self._local_sem is not None:
            try:
                self._local_sem.release()
            except ValueError:
                pass

    def slot(self, *, limit: int):
        """async context manager: захватывает слот на время блока."""
        return _Slot(self, limit)

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass


class _Slot:
    def __init__(self, limiter: RedisConcurrencyLimiter, limit: int):
        self._limiter = limiter
        self._limit = limit
        self._token: str | None = None

    async def __aenter__(self):
        self._token = await self._limiter.acquire(limit=self._limit)
        return self._token

    async def __aexit__(self, *exc):
        await self._limiter.release(self._token)
        return False


# Глобальный singleton для постинга — один на процесс, все run-task-и шарят.
# stale_ttl с запасом на самый медленный пост (CF warm + Tier2 + retry).
posting_limiter = RedisConcurrencyLimiter("posting", stale_ttl_s=180.0)
