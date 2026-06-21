"""/admin/api/audit-log — лента audit-событий (super_admin only)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import require_super_admin
from api.admin.schemas.audit import ActorBrief, AuditEntry, AuditListResponse
from core.db import get_db_read
from domain.audit.service import get_unique_actions, list_audit
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/audit-log", tags=["audit-log"])


@router.get("", response_model=AuditListResponse)
async def list_endpoint(
    actor_id: int | None = Query(default=None),
    action: str | None = Query(default=None, max_length=64),
    action_prefix: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=32),
    resource_id: int | None = Query(default=None),
    after_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> AuditListResponse:
    rows = await list_audit(
        session,
        actor_id=actor_id,
        action=action,
        action_prefix=action_prefix,
        resource_type=resource_type,
        resource_id=resource_id,
        after_id=after_id,
        limit=limit,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    # actor подгрузим одним запросом по уникальным id (без N+1)
    actor_ids = {r.actor_user_id for r in rows if r.actor_user_id}
    actors_map: dict[int, AdminUser] = {}
    if actor_ids:
        actors = (
            await session.execute(select(AdminUser).where(AdminUser.id.in_(actor_ids)))
        ).scalars().all()
        actors_map = {u.id: u for u in actors}

    items: list[AuditEntry] = []
    for r in rows:
        actor_obj = actors_map.get(r.actor_user_id) if r.actor_user_id else None
        items.append(AuditEntry(
            id=r.id,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            changes=r.changes,
            ip=r.ip,
            user_agent=r.user_agent,
            created_at=r.created_at,
            actor=ActorBrief.model_validate(actor_obj) if actor_obj else None,
        ))
    return AuditListResponse(items=items, has_more=has_more)


@router.get("/actions", response_model=list[str])
async def list_actions(
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> list[str]:
    """Уникальные action-ы для UI dropdown-а."""
    return await get_unique_actions(session)
