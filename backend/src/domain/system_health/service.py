"""System health probes. Все best-effort — никогда не падаем целиком."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def _probe_redis_and_queues() -> dict[str, Any]:
    """Redis ping + глубина Celery-очереди (base + priority sub-queues)."""
    out: dict[str, Any] = {"redis_ok": False, "celery_queue_depth": None}
    try:
        import redis.asyncio as aioredis

        from core.config import settings

        r = aioredis.from_url(settings.REDIS_URL)
        try:
            out["redis_ok"] = bool(await r.ping())
            # Celery на Redis: base-очередь 'celery' + priority варианты
            # 'celery\x06\x16N'. Суммируем LLEN по всем подходящим ключам.
            total = 0
            try:
                keys = await r.keys("celery*")
                for k in keys:
                    try:
                        if (await r.type(k)) == b"list":
                            total += int(await r.llen(k))
                    except Exception:
                        pass
                out["celery_queue_depth"] = total
            except Exception:
                out["celery_queue_depth"] = None
        finally:
            await r.aclose()
    except Exception as e:
        log.debug("health.redis.failed", error=str(e))
    return out


def _probe_db_pool() -> dict[str, Any]:
    """Состояние write-пула SQLAlchemy."""
    try:
        from core.db import write_engine

        pool = write_engine.pool
        return {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "checked_in": pool.checkedin(),
        }
    except Exception as e:
        log.debug("health.dbpool.failed", error=str(e))
        return {}


async def _probe_cf_browser() -> bool:
    """CF Tier 3: установлен ли Patchright (+ браузер) в этом процессе.
    Заменил FlareSolverr — CF теперь проходим реальным браузером."""
    try:
        from infrastructure.cf_browser import is_browser_available

        return bool(is_browser_available())
    except Exception:
        return False


async def _probe_proxies(session: AsyncSession) -> dict[str, int]:
    from infrastructure.db.models import Proxy

    now = datetime.now(UTC)
    row = (await session.execute(
        select(
            func.count(Proxy.id).label("total"),
            func.count(Proxy.id).filter(Proxy.is_active.is_(True)).label("active"),
            func.count(Proxy.id).filter(
                Proxy.locked_until.is_not(None), Proxy.locked_until > now
            ).label("locked"),
            func.count(Proxy.id).filter(Proxy.status == "down").label("down"),
        )
    )).one()
    return {
        "total": int(row.total), "active": int(row.active),
        "locked": int(row.locked), "down": int(row.down),
    }


async def _probe_runs_batches(session: AsyncSession) -> dict[str, int]:
    from infrastructure.db.models import PostingRun, WpImportBatch

    active_run_statuses = ("unpacking", "queued", "running", "paused", "scheduled")
    runs_active = int((await session.execute(
        select(func.count(PostingRun.id)).where(
            PostingRun.deleted_at.is_(None),
            PostingRun.status.in_(active_run_statuses),
        )
    )).scalar_one())
    batches_validating = int((await session.execute(
        select(func.count(WpImportBatch.id)).where(
            WpImportBatch.status == "validating",
        )
    )).scalar_one())
    return {"runs_active": runs_active, "batches_validating": batches_validating}


async def _probe_recent_failures(session: AsyncSession, limit: int = 10) -> list[dict]:
    from infrastructure.db.models import TextItem, WpSite

    since = datetime.now(UTC) - timedelta(hours=24)
    rows = (await session.execute(
        select(
            TextItem.id, TextItem.last_error, TextItem.posting_run_id,
            TextItem.updated_at, WpSite.domain,
        )
        .outerjoin(WpSite, WpSite.id == TextItem.site_id)
        .where(TextItem.status == "failed", TextItem.updated_at >= since)
        .order_by(TextItem.updated_at.desc())
        .limit(limit)
    )).all()
    return [
        {
            "text_item_id": r.id,
            "domain": r.domain,
            "run_id": r.posting_run_id,
            "error": (r.last_error or "")[:200],
            "at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


async def gather_system_health(session: AsyncSession) -> dict[str, Any]:
    """Полный снимок инфра-здоровья. Каждый блок best-effort."""
    redis_q = await _probe_redis_and_queues()
    proxies = await _probe_proxies(session)
    runs = await _probe_runs_batches(session)
    failures = await _probe_recent_failures(session)
    cf_browser_ok = await _probe_cf_browser()
    db_pool = _probe_db_pool()

    return {
        "redis_ok": redis_q["redis_ok"],
        "celery_queue_depth": redis_q["celery_queue_depth"],
        "cf_browser_ok": cf_browser_ok,
        "db_pool": db_pool,
        "proxies": proxies,
        "runs_active": runs["runs_active"],
        "batches_validating": runs["batches_validating"],
        "recent_failures": failures,
    }
