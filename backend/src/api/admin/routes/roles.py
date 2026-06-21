"""/admin/api/roles + /admin/api/permissions — управление ролями. Только super_admin."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.admin_objects import (
    CreateRoleRequest,
    PermissionResponse,
    RoleResponse,
    UpdateRoleRequest,
)
from core.db import get_db_read, get_db_write
from infrastructure.db.models import AdminPage, AdminPermission, AdminRole, AdminUser

log = structlog.get_logger(__name__)

roles_router = APIRouter(prefix="/roles", tags=["roles"])
permissions_router = APIRouter(prefix="/permissions", tags=["permissions"])


# ─── Permissions ──────────────────────────────────────────────────────


@permissions_router.get("", response_model=list[PermissionResponse])
async def list_permissions(
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> list[PermissionResponse]:
    rows = (
        await session.execute(select(AdminPermission).order_by(AdminPermission.code))
    ).scalars().all()
    return [PermissionResponse.model_validate(p) for p in rows]


# ─── Roles ────────────────────────────────────────────────────────────


async def _load_role(session: AsyncSession, role_id: int) -> AdminRole | None:
    stmt = (
        select(AdminRole)
        .where(AdminRole.id == role_id)
        .options(selectinload(AdminRole.permissions), selectinload(AdminRole.pages))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@roles_router.get("", response_model=list[RoleResponse])
async def list_roles(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[RoleResponse]:
    """
    super_admin → все роли
    group_admin → только assignable роли (для назначения юзерам своей группы)
    user → 403
    """
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    stmt = (
        select(AdminRole)
        .order_by(AdminRole.name)
        .options(selectinload(AdminRole.permissions), selectinload(AdminRole.pages))
    )
    if not viewer.is_super_admin:
        # group_admin видит только то, что super_admin разрешил делегировать
        stmt = stmt.where(AdminRole.is_assignable_by_group_admin.is_(True), AdminRole.is_active.is_(True))

    rows = (await session.execute(stmt)).scalars().all()
    return [RoleResponse.model_validate(r) for r in rows]


@roles_router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: CreateRoleRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> RoleResponse:
    role = AdminRole(
        name=payload.name.strip(),
        description=payload.description,
        is_active=True,
        is_system=False,
        is_assignable_by_group_admin=payload.is_assignable_by_group_admin,
    )

    if payload.permission_ids:
        perms = (
            await session.execute(select(AdminPermission).where(AdminPermission.id.in_(payload.permission_ids)))
        ).scalars().all()
        role.permissions = list(perms)

    if payload.page_ids:
        pages = (
            await session.execute(select(AdminPage).where(AdminPage.id.in_(payload.page_ids)))
        ).scalars().all()
        role.pages = list(pages)

    session.add(role)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role name already exists") from None

    loaded = await _load_role(session, role.id)
    log.info("roles.created", actor_id=actor.id, role_id=role.id)
    return RoleResponse.model_validate(loaded)


@roles_router.patch("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    payload: UpdateRoleRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> RoleResponse:
    role = await _load_role(session, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    # super_admin роль — нельзя менять permissions/pages
    if role.name == "super_admin":
        if payload.permission_ids is not None or payload.page_ids is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="super_admin role permissions and pages are immutable",
            )

    if payload.name is not None and not role.is_system:
        role.name = payload.name.strip()
    if payload.description is not None:
        role.description = payload.description
    if payload.is_active is not None and not role.is_system:
        role.is_active = payload.is_active
    if payload.is_assignable_by_group_admin is not None:
        # super_admin может включать/выключать делегирование для любой роли,
        # кроме super_admin (которая сама никогда не делегируется).
        if role.name != "super_admin":
            role.is_assignable_by_group_admin = payload.is_assignable_by_group_admin

    if payload.permission_ids is not None:
        perms = (
            await session.execute(select(AdminPermission).where(AdminPermission.id.in_(payload.permission_ids)))
        ).scalars().all()
        role.permissions = list(perms)

    if payload.page_ids is not None:
        pages = (
            await session.execute(select(AdminPage).where(AdminPage.id.in_(payload.page_ids)))
        ).scalars().all()
        role.pages = list(pages)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role name already exists") from None

    loaded = await _load_role(session, role_id)
    log.info("roles.updated", actor_id=actor.id, role_id=role_id)
    return RoleResponse.model_validate(loaded)


@roles_router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    role = await _load_role(session, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete system role")
    await session.delete(role)
    await session.commit()
    log.info("roles.deleted", actor_id=actor.id, role_id=role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
