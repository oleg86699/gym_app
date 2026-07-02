"""/admin/api/groups — CRUD групп + detail page (members + projects)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.admin_objects import (
    CreateGroupRequest,
    GroupListResponse,
    GroupResponse,
    ProjectChip,
    UpdateGroupRequest,
)
from api.admin.schemas.projects import ProjectResponse
from api.admin.schemas.users import UserResponse
from core.db import get_db_read, get_db_write
from infrastructure.db.models import (
    AdminGroup,
    AdminUser,
    Project,
    group_projects,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupListResponse])
async def list_groups(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[GroupListResponse]:
    """
    Scope:
    - super_admin → все группы
    - group_admin → только своя группа (для share-форм)
    - user → 403 (информационная защита: не показываем структуру компании)
    """
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    from sqlalchemy import func

    stmt = select(AdminGroup).where(AdminGroup.deleted_at.is_(None)).order_by(AdminGroup.name)
    if not viewer.is_super_admin:
        # group_admin видит только свою группу
        if viewer.group_id is None:
            return []
        stmt = stmt.where(AdminGroup.id == viewer.group_id)

    groups = (await session.execute(stmt)).scalars().all()

    if not groups:
        return []

    group_ids = [g.id for g in groups]

    # Members count в одном запросе
    members_counts: dict[int, int] = {}
    rows = (
        await session.execute(
            select(AdminUser.group_id, func.count(AdminUser.id))
            .where(AdminUser.group_id.in_(group_ids), AdminUser.deleted_at.is_(None))
            .group_by(AdminUser.group_id)
        )
    ).all()
    for gid, cnt in rows:
        members_counts[gid] = cnt

    # Owned projects (owner_group_id == X)
    owned_map: dict[int, list[ProjectChip]] = {gid: [] for gid in group_ids}
    rows = (
        await session.execute(
            select(Project)
            .where(Project.owner_group_id.in_(group_ids), Project.deleted_at.is_(None))
            .order_by(Project.name)
        )
    ).scalars().all()
    for p in rows:
        owned_map.setdefault(p.owner_group_id, []).append(ProjectChip.model_validate(p))

    # Shared projects (через group_projects)
    shared_map: dict[int, list[ProjectChip]] = {gid: [] for gid in group_ids}
    rows = (
        await session.execute(
            select(group_projects.c.group_id, Project)
            .join(Project, Project.id == group_projects.c.project_id)
            .where(group_projects.c.group_id.in_(group_ids), Project.deleted_at.is_(None))
            .order_by(Project.name)
        )
    ).all()
    for gid, p in rows:
        shared_map.setdefault(gid, []).append(ProjectChip.model_validate(p))

    return [
        GroupListResponse(
            id=g.id,
            name=g.name,
            description=g.description,
            is_active=g.is_active,
            created_at=g.created_at,
            members_count=members_counts.get(g.id, 0),
            owned_projects=owned_map.get(g.id, []),
            shared_projects=shared_map.get(g.id, []),
        )
        for g in groups
    ]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: CreateGroupRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> GroupResponse:
    group = AdminGroup(
        name=payload.name.strip(),
        description=payload.description,
        is_active=payload.is_active,
    )
    session.add(group)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists") from None

    log.info("groups.created", actor_id=actor.id, group_id=group.id, name=group.name)
    return GroupResponse.model_validate(group)


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: int,
    payload: UpdateGroupRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> GroupResponse:
    group = (
        await session.execute(select(AdminGroup).where(AdminGroup.id == group_id, AdminGroup.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    if payload.name is not None:
        group.name = payload.name.strip()
    if payload.description is not None:
        group.description = payload.description
    if payload.is_active is not None:
        group.is_active = payload.is_active
    # tag-access RBAC: потолок разрешённых команде тегов (super_admin only —
    # эндпоинт под require_super_admin). null = снять ограничение.
    if "allowed_tags" in payload.model_fields_set:
        group.allowed_tags = payload.allowed_tags

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists") from None

    # Полная замена списка projects, явно расшаренных группе
    if payload.shared_project_ids is not None:
        from sqlalchemy import delete, insert

        await session.execute(delete(group_projects).where(group_projects.c.group_id == group.id))
        if payload.shared_project_ids:
            await session.execute(
                insert(group_projects),
                [{"group_id": group.id, "project_id": pid} for pid in payload.shared_project_ids],
            )
        await session.commit()
        log.info(
            "groups.shared_projects_set",
            actor_id=actor.id,
            group_id=group.id,
            count=len(payload.shared_project_ids),
        )

    log.info("groups.updated", actor_id=actor.id, group_id=group.id)
    return GroupResponse.model_validate(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    group = (
        await session.execute(select(AdminGroup).where(AdminGroup.id == group_id, AdminGroup.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group.deleted_at = datetime.now(UTC)
    group.is_active = False
    await session.commit()
    log.info("groups.deleted", actor_id=actor.id, group_id=group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Detail endpoints ────────────────────────────────────────────────


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: int,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> GroupResponse:
    group = (
        await session.execute(
            select(AdminGroup).where(AdminGroup.id == group_id, AdminGroup.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return GroupResponse.model_validate(group)


@router.get("/{group_id}/members", response_model=list[UserResponse])
async def list_group_members(
    group_id: int,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> list[UserResponse]:
    stmt = (
        select(AdminUser)
        .where(AdminUser.group_id == group_id, AdminUser.deleted_at.is_(None))
        .options(selectinload(AdminUser.group), selectinload(AdminUser.roles))
        .order_by(AdminUser.username)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [UserResponse.model_validate(u) for u in rows]


@router.get("/{group_id}/projects", response_model=list[ProjectResponse])
async def list_group_projects(
    group_id: int,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> list[ProjectResponse]:
    """Все проекты, доступные этой группе: owner_group=X или shared с группой."""
    stmt = (
        select(Project)
        .where(
            Project.deleted_at.is_(None),
            (Project.owner_group_id == group_id)
            | (Project.id.in_(select(group_projects.c.project_id).where(group_projects.c.group_id == group_id))),
        )
        .options(
            selectinload(Project.owner),
            selectinload(Project.owner_group),
            selectinload(Project.shared_with_users),
            selectinload(Project.shared_with_groups),
        )
        .order_by(Project.created_at.desc())
    )
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [ProjectResponse.model_validate(p) for p in rows]
