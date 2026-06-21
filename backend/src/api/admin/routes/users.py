"""/admin/api/users — CRUD пользователей с учётом scope."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user
from api.admin.schemas.users import (
    CreateUserRequest,
    ProjectBrief,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserDetailResponse,
    UserResponse,
)
from api.common.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PaginatedResponse,
    encode_cursor,
)
from core.db import get_db_read, get_db_write
from domain.users.service import (
    UserConflictError,
    can_manage_user,
    can_view_user,
    create_user,
    get_user,
    get_user_shared_projects,
    list_users,
    set_password,
    set_user_direct_pages,
    set_user_projects,
    soft_delete_user,
    update_user,
)
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    import base64
    import json

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        return int(json.loads(raw)["after_id"])
    except Exception:
        return None


@router.get("", response_model=PaginatedResponse[UserResponse])
async def list_users_endpoint(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    group_id: int | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> PaginatedResponse[UserResponse]:
    after = _decode_cursor(cursor)
    rows = await list_users(
        session,
        viewer=viewer,
        after_id=after,
        limit=limit,
        group_id=group_id,
        search=search,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [UserResponse.model_validate(u) for u in rows]
    next_cursor = encode_cursor(rows[-1].id) if has_more and rows else None
    return PaginatedResponse[UserResponse](items=items, next_cursor=next_cursor, has_more=has_more)


async def _validate_role_assignment(
    session: AsyncSession, viewer: AdminUser, role_ids: list[int]
) -> None:
    """group_admin может назначать только assignable роли."""
    if viewer.is_super_admin or not role_ids:
        return
    from sqlalchemy import select as _select

    from infrastructure.db.models import AdminRole

    rows = (await session.execute(_select(AdminRole).where(AdminRole.id.in_(role_ids)))).scalars().all()
    bad = [r.name for r in rows if not r.is_assignable_by_group_admin]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"group_admin cannot assign these roles: {', '.join(bad)}",
        )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(
    payload: CreateUserRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> UserResponse:
    # Только super_admin или group_admin могут создавать
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    # group_admin может создавать только в своей группе
    target_group_id = payload.group_id
    if viewer.is_group_admin and not viewer.is_super_admin:
        if target_group_id is None or target_group_id != viewer.group_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="group_admin can only create users in own group",
            )

    await _validate_role_assignment(session, viewer, payload.role_ids)

    try:
        user = await create_user(
            session,
            username=payload.username,
            password=payload.password,
            email=str(payload.email) if payload.email else None,
            full_name=payload.full_name,
            group_id=target_group_id,
            role_ids=payload.role_ids,
            is_active=payload.is_active,
        )
    except UserConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Conflict: {e}") from e

    log.info("users.created", actor_id=viewer.id, target_id=user.id, target_username=user.username)
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user_endpoint(
    user_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> UserDetailResponse:
    target = await get_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_view_user(viewer, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this user")

    shared = await get_user_shared_projects(session, user_id)

    base = UserResponse.model_validate(target).model_dump()
    return UserDetailResponse(
        **base,
        shared_projects=[ProjectBrief.model_validate(p) for p in shared],
        direct_page_ids=[p.id for p in target.direct_pages],
    )


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user_endpoint(
    user_id: int,
    payload: UpdateUserRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> UserResponse:
    target = await get_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(viewer, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot edit this user")

    # group_admin не может перемещать в чужую группу
    if not viewer.is_super_admin and viewer.is_group_admin and payload.group_id is not None:
        if payload.group_id != viewer.group_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="group_admin cannot reassign users outside own group",
            )

    if payload.role_ids is not None:
        await _validate_role_assignment(session, viewer, payload.role_ids)

    # Семантика обновления group_id:
    # - is_remove_from_group=true → выставить NULL
    # - group_id указан и положительный → присвоить
    # - иначе → не трогать
    if payload.is_remove_from_group:
        group_id_arg: int | None = None
    elif payload.group_id is not None:
        group_id_arg = payload.group_id
    else:
        group_id_arg = ...  # type: ignore[assignment]

    try:
        updated = await update_user(
            session,
            user=target,
            username=payload.username,
            full_name=payload.full_name,
            email=str(payload.email) if payload.email else (None if payload.email is None else ""),
            is_active=payload.is_active,
            password=payload.password,
            group_id=group_id_arg,  # type: ignore[arg-type]
            role_ids=payload.role_ids,
        )
    except UserConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Conflict: {e}") from e

    # Обновить project-share (если передано) — только super_admin или owner/group_admin проекта в scope.
    # Здесь мы доверяем checker-у можем-править-юзера; индивидуальный share меняется только если ты управляешь юзером.
    if payload.project_ids is not None:
        await set_user_projects(session, user_id, payload.project_ids)
        log.info("users.project_access_updated", actor_id=viewer.id, target_id=user_id, count=len(payload.project_ids))

    # Индивидуальные страницы — только super_admin (так задумано в RBAC)
    if payload.page_ids is not None:
        if not viewer.is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super_admin can assign direct pages to users",
            )
        await set_user_direct_pages(session, user_id, payload.page_ids)
        log.info("users.direct_pages_updated", actor_id=viewer.id, target_id=user_id, count=len(payload.page_ids))

    log.info("users.updated", actor_id=viewer.id, target_id=user_id)
    # Перечитываем для актуальных связей
    refreshed = await get_user(session, user_id)
    shared = await get_user_shared_projects(session, user_id)
    return UserResponse.model_validate(refreshed)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_endpoint(
    user_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    target = await get_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(viewer, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete this user")
    if target.is_super_admin:
        # Защита: нельзя удалить последнего super_admin
        from sqlalchemy import func, select

        from infrastructure.db.models import AdminRole, user_roles

        count_q = (
            select(func.count(AdminUser.id))
            .join(user_roles, AdminUser.id == user_roles.c.admin_user_id)
            .join(AdminRole, AdminRole.id == user_roles.c.role_id)
            .where(AdminRole.name == "super_admin", AdminUser.deleted_at.is_(None))
        )
        count = (await session.execute(count_q)).scalar_one()
        if count <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last super_admin")

    await soft_delete_user(session, target)
    log.info("users.deleted", actor_id=viewer.id, target_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password_endpoint(
    user_id: int,
    payload: ResetPasswordRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    target = await get_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(viewer, target):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot reset password for this user")
    await set_password(session, target, payload.new_password)
    log.info("users.password_reset", actor_id=viewer.id, target_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
