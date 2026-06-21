"""
Async SQLAlchemy engines + session factories.

Две фабрики: get_db_write (для мутаций) и get_db_read (для чтения).
На этапе 1 обе смотрят на один и тот же DATABASE_URL через PgBouncer.
В будущем DATABASE_READ_URL сможет указать на read replica без изменения
кода в сервисах. См. ADR-011 категория Б.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import settings

# ─── Engines ──────────────────────────────────────────────────────────

# PgBouncer в transaction mode не поддерживает prepared statements
# (которые asyncpg кеширует по умолчанию). Отключаем кеш — иначе будут
# DuplicatePreparedStatementError при горячем переиспользовании коннектов.
# Реальный пул живёт в PgBouncer (DEFAULT_POOL_SIZE=40, см. docker-compose).
_ASYNCPG_PGBOUNCER_CONNECT_ARGS = {
    "statement_cache_size": 0,
    "prepared_statement_cache_size": 0,
}


def _make_engine(url: str):
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=False,
        connect_args=_ASYNCPG_PGBOUNCER_CONNECT_ARGS,
    )


write_engine = _make_engine(settings.DATABASE_URL)

read_engine = (
    _make_engine(settings.effective_read_url)
    if settings.DATABASE_READ_URL and settings.DATABASE_READ_URL != settings.DATABASE_URL
    else write_engine
)

# ─── Session factories ────────────────────────────────────────────────

WriteSession = async_sessionmaker(
    bind=write_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

ReadSession = async_sessionmaker(
    bind=read_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# ─── FastAPI dependencies ─────────────────────────────────────────────


async def get_db_write() -> AsyncIterator[AsyncSession]:
    """Сессия для пишущих операций."""
    async with WriteSession() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_db_read() -> AsyncIterator[AsyncSession]:
    """
    Сессия для read-only операций. На этапе 1 идёт в тот же primary.
    Сервисы должны использовать её по умолчанию; писать только через get_db_write.
    """
    async with ReadSession() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping() -> bool:
    """Проверить, что БД отвечает. Используется readiness-probe."""
    from sqlalchemy import text

    try:
        async with ReadSession() as session:
            result = await session.execute(text("SELECT 1"))
            return result.scalar() == 1
    except Exception:
        return False
