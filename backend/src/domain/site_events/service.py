"""Site events service — запись и чтение истории ошибок по сайтам.

record_site_event — best-effort: ошибка логирования НЕ должна ронять
основной flow валидации/постинга.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.db.models import SiteEvent

log = structlog.get_logger(__name__)


async def record_site_event(
    session: AsyncSession,
    *,
    site_id: int,
    source: str,                 # 'validation' | 'posting'
    error_kind: str,
    error_message: str | None = None,
    credential_id: int | None = None,
    posting_run_id: int | None = None,
    proxy_id: int | None = None,
) -> None:
    """Записать failure-событие. Best-effort — глотаем ошибки записи.

    НЕ коммитит сам — рассчитан на вызов внутри существующей транзакции
    (caller сделает commit). Если нужна изоляция — оберни в свою сессию.
    """
    try:
        session.add(SiteEvent(
            site_id=site_id,
            credential_id=credential_id,
            source=source,
            error_kind=error_kind,
            error_message=(error_message or "")[:500] or None,
            posting_run_id=posting_run_id,
            proxy_id=proxy_id,
        ))
    except Exception as e:
        log.warning("site_event.record_failed", site_id=site_id, error=str(e))


async def list_site_events(
    session: AsyncSession, *, site_id: int, limit: int = 50
) -> list[SiteEvent]:
    """Последние события сайта (для UI вкладки «История ошибок»)."""
    rows = (await session.execute(
        select(SiteEvent)
        .where(SiteEvent.site_id == site_id)
        .order_by(SiteEvent.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)
