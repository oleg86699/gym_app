"""
Audit log service.

Использование из API:

    from domain.audit.service import record

    await record(
        session,
        actor=viewer,
        action="users.delete",
        resource_type="user",
        resource_id=user_id,
        request=request,                 # FastAPI Request — для ip/UA
        changes={"prev_email": old_email},
    )

Вызывать ПОСЛЕ успешного commit-а основного действия — иначе можно
залогировать то, чего не было. Чтобы не упасть, если запись audit упала
(например, БД на доли секунды отвалилась) — глотаем исключения с warning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Request
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from infrastructure.db.models import AdminUser, AuditLog

log = structlog.get_logger(__name__)


def _extract_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # x-forwarded-for (через nginx)
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:45]
    if request.client and request.client.host:
        return request.client.host[:45]
    return None


def _extract_ua(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("user-agent")
    return ua[:500] if ua else None


async def record(
    session: AsyncSession,
    *,
    actor: AdminUser | None,
    action: str,
    resource_type: str | None = None,
    resource_id: int | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """
    Записать audit entry. Не блокирует основной flow — при ошибке логируем
    в structlog warning и идём дальше.
    """
    try:
        entry = AuditLog(
            actor_user_id=actor.id if actor else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            changes=changes,
            ip=_extract_ip(request),
            user_agent=_extract_ua(request),
            created_at=datetime.now(UTC),
        )
        session.add(entry)
        await session.commit()
    except Exception as e:
        log.warning("audit.record_failed", action=action, error=str(e))


# ─── Queries ─────────────────────────────────────────────────────────


async def list_audit(
    session: AsyncSession,
    *,
    actor_id: int | None = None,
    action: str | None = None,
    action_prefix: str | None = None,
    resource_type: str | None = None,
    resource_id: int | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .options(selectinload(AuditLog.__mapper__.relationships).joinedload(*[]))  # placeholder
        .order_by(desc(AuditLog.id))
        .limit(limit + 1)
    )
    # Простой select без selectinload — actor подгрузим отдельным запросом ниже
    # для UI. Сохраняем простоту, без отдельной relationship-декларации.
    stmt = select(AuditLog).order_by(desc(AuditLog.id)).limit(limit + 1)

    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if action_prefix is not None:
        stmt = stmt.where(AuditLog.action.like(f"{action_prefix}%"))
    if resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if after_id is not None:
        stmt = stmt.where(AuditLog.id < after_id)

    return list((await session.execute(stmt)).scalars().all())


async def get_unique_actions(session: AsyncSession) -> list[str]:
    """Топ-100 уникальных action-ов — для UI dropdown-а."""
    stmt = (
        select(AuditLog.action, func.count(AuditLog.id).label("n"))
        .group_by(AuditLog.action)
        .order_by(desc("n"))
        .limit(100)
    )
    return [str(r[0]) for r in (await session.execute(stmt)).all()]
