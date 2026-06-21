"""/admin/api/pages — управление матрицей доступа к страницам."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.admin_objects import (
    PageResponse,
    PageWithAssignments,
    UpdatePageRequest,
)
from core.db import get_db_read, get_db_write
from infrastructure.db.models import AdminPage, AdminRole, AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/pages", tags=["pages"])


# ─── /pages/me — какие страницы доступны мне (для меню UI) ───────────
# ВАЖНО: объявлен ДО /pages/{page_id}, иначе "me" попадёт в path param.


@router.get("/me", response_model=list[str])
async def my_pages(
    user: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[str]:
    if user.is_super_admin:
        rows = (
            await session.execute(
                select(AdminPage.path).where(AdminPage.is_active.is_(True)).order_by(AdminPage.path)
            )
        ).scalars().all()
        return list(rows)
    return sorted(user.accessible_page_paths())


# ─── Полный список страниц + назначения ──────────────────────────────


@router.get("", response_model=list[PageWithAssignments])
async def list_pages(
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> list[PageWithAssignments]:
    rows = (
        await session.execute(
            select(AdminPage)
            .order_by(AdminPage.path)
            .options(selectinload(AdminPage.roles), selectinload(AdminPage.users))
        )
    ).scalars().all()

    out: list[PageWithAssignments] = []
    for p in rows:
        out.append(
            PageWithAssignments(
                id=p.id,
                path=p.path,
                name=p.name,
                description=p.description,
                is_active=p.is_active,
                created_at=p.created_at,
                role_ids=[r.id for r in p.roles],
                user_ids=[u.id for u in p.users],
            )
        )
    return out


@router.get("/{page_id}", response_model=PageResponse)
async def get_page(
    page_id: int,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> PageResponse:
    page = (await session.execute(select(AdminPage).where(AdminPage.id == page_id))).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return PageResponse.model_validate(page)


@router.patch("/{page_id}", response_model=PageWithAssignments)
async def update_page(
    page_id: int,
    payload: UpdatePageRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> PageWithAssignments:
    page = (
        await session.execute(
            select(AdminPage)
            .where(AdminPage.id == page_id)
            .options(selectinload(AdminPage.roles), selectinload(AdminPage.users))
        )
    ).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    if payload.name is not None:
        page.name = payload.name
    if payload.description is not None:
        page.description = payload.description
    if payload.is_active is not None:
        page.is_active = payload.is_active

    if payload.role_ids is not None:
        roles = (
            await session.execute(select(AdminRole).where(AdminRole.id.in_(payload.role_ids)))
        ).scalars().all()
        page.roles = list(roles)

    if payload.user_ids is not None:
        users = (
            await session.execute(select(AdminUser).where(AdminUser.id.in_(payload.user_ids)))
        ).scalars().all()
        page.users = list(users)

    await session.commit()

    log.info("pages.updated", actor_id=actor.id, page_id=page_id)
    return PageWithAssignments(
        id=page.id,
        path=page.path,
        name=page.name,
        description=page.description,
        is_active=page.is_active,
        created_at=page.created_at,
        role_ids=[r.id for r in page.roles],
        user_ids=[u.id for u in page.users],
    )
