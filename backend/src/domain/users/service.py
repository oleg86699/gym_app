"""
User CRUD service. Учитывает scope видимости (super_admin → все,
group_admin → своя группа, manager → только себя).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.security import hash_password
from infrastructure.db.models import AdminGroup, AdminPage, AdminRole, AdminUser, Project


# ─── Visibility ───────────────────────────────────────────────────────


def can_view_user(viewer: AdminUser, target: AdminUser) -> bool:
    if viewer.is_super_admin:
        return True
    if viewer.is_group_admin and viewer.group_id and target.group_id == viewer.group_id:
        return True
    return viewer.id == target.id


def can_manage_user(viewer: AdminUser, target: AdminUser) -> bool:
    if viewer.id == target.id:
        # Можно править свой профиль через /me, не через /users; здесь говорим "нет"
        return False
    if viewer.is_super_admin:
        return True
    if viewer.is_group_admin and viewer.group_id and target.group_id == viewer.group_id:
        # group_admin не может трогать super_admin-ов
        return not target.is_super_admin
    return False


# ─── Queries ──────────────────────────────────────────────────────────


async def list_users(
    session: AsyncSession,
    *,
    viewer: AdminUser,
    after_id: int | None = None,
    limit: int = 50,
    group_id: int | None = None,
    search: str | None = None,
) -> list[AdminUser]:
    """Список юзеров с учётом scope viewer. Временные supplier-аккаунты
    (is_temporary) сюда НЕ попадают — они только на странице «Доступы поставщиков»."""
    stmt = (
        select(AdminUser)
        .where(AdminUser.deleted_at.is_(None), AdminUser.is_temporary.is_(False))
        .options(
            selectinload(AdminUser.group),
            selectinload(AdminUser.roles),
        )
        .order_by(AdminUser.id.asc())
        .limit(limit + 1)
    )

    if after_id:
        stmt = stmt.where(AdminUser.id > after_id)

    if group_id is not None:
        stmt = stmt.where(AdminUser.group_id == group_id)

    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where((AdminUser.username.ilike(like)) | (AdminUser.email.ilike(like)))

    # Scope-фильтр
    if viewer.is_super_admin:
        pass
    elif viewer.is_group_admin and viewer.group_id is not None:
        # group_admin видит всю свою группу
        stmt = stmt.where(AdminUser.group_id == viewer.group_id)
    elif viewer.group_id is not None:
        # обычный user с группой видит свою команду (для share-форм)
        stmt = stmt.where(AdminUser.group_id == viewer.group_id)
    else:
        # user без группы видит только себя
        stmt = stmt.where(AdminUser.id == viewer.id)

    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def get_user(session: AsyncSession, user_id: int) -> AdminUser | None:
    stmt = (
        select(AdminUser)
        .where(AdminUser.id == user_id, AdminUser.deleted_at.is_(None))
        .options(
            selectinload(AdminUser.group),
            selectinload(AdminUser.roles),
            selectinload(AdminUser.direct_pages),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_user_direct_pages(
    session: AsyncSession, user_id: int, page_ids: list[int]
) -> None:
    """Полностью заменяет список индивидуальных страниц юзера (admin_user_pages)."""
    from sqlalchemy import delete, insert

    from infrastructure.db.models import user_pages

    await session.execute(delete(user_pages).where(user_pages.c.admin_user_id == user_id))
    if page_ids:
        await session.execute(
            insert(user_pages),
            [{"admin_user_id": user_id, "page_id": pid} for pid in page_ids],
        )
    await session.commit()


async def get_user_shared_projects(session: AsyncSession, user_id: int) -> list[Project]:
    """Список проектов, к которым юзер имеет индивидуальный shared доступ."""
    from infrastructure.db.models import user_projects

    stmt = (
        select(Project)
        .join(user_projects, user_projects.c.project_id == Project.id)
        .where(
            user_projects.c.admin_user_id == user_id,
            Project.deleted_at.is_(None),
        )
        .order_by(Project.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_user_projects(
    session: AsyncSession, user_id: int, project_ids: list[int]
) -> None:
    """Полностью заменяет индивидуальный shared список проектов для юзера."""
    from sqlalchemy import delete, insert

    from infrastructure.db.models import user_projects

    # Снести все текущие
    await session.execute(delete(user_projects).where(user_projects.c.admin_user_id == user_id))
    # Вставить новые
    if project_ids:
        await session.execute(
            insert(user_projects),
            [{"admin_user_id": user_id, "project_id": pid} for pid in project_ids],
        )
    await session.commit()


# ─── Mutations ────────────────────────────────────────────────────────


class UserConflictError(Exception):
    """Username или email уже занят."""


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    email: str | None,
    full_name: str | None,
    group_id: int | None,
    role_ids: list[int],
    is_active: bool = True,
) -> AdminUser:
    # Дефолтная роль 'user', если ничего не передали
    if not role_ids:
        default_role = (
            await session.execute(select(AdminRole).where(AdminRole.name == "user"))
        ).scalar_one_or_none()
        if default_role is not None:
            role_ids = [default_role.id]

    user = AdminUser(
        username=username.strip(),
        email=(email.strip() if email else None) or None,
        hashed_password=hash_password(password),
        full_name=full_name,
        group_id=group_id,
        is_active=is_active,
    )
    if role_ids:
        roles = (await session.execute(select(AdminRole).where(AdminRole.id.in_(role_ids)))).scalars().all()
        user.roles = list(roles)

    session.add(user)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise UserConflictError(str(e.orig)) from e

    return await get_user(session, user.id)  # type: ignore[return-value]


async def update_user(
    session: AsyncSession,
    *,
    user: AdminUser,
    username: str | None = None,
    full_name: str | None = None,
    email: str | None = None,
    is_active: bool | None = None,
    password: str | None = None,
    group_id: int | None = ...,  # type: ignore[assignment]
    role_ids: list[int] | None = None,
) -> AdminUser:
    if username is not None and username.strip() != user.username:
        user.username = username.strip()
    if full_name is not None:
        user.full_name = full_name
    if email is not None:
        user.email = email or None
    if is_active is not None:
        user.is_active = is_active
    if password is not None and password:
        user.hashed_password = hash_password(password)
    if group_id is not ...:  # type: ignore[comparison-overlap]
        user.group_id = group_id  # type: ignore[assignment]

    if role_ids is not None:
        roles = (await session.execute(select(AdminRole).where(AdminRole.id.in_(role_ids)))).scalars().all()
        user.roles = list(roles)

    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise UserConflictError(str(e.orig)) from e

    return await get_user(session, user.id)  # type: ignore[return-value]


async def soft_delete_user(session: AsyncSession, user: AdminUser) -> None:
    from datetime import UTC, datetime

    user.deleted_at = datetime.now(UTC)
    user.is_active = False
    await session.commit()


async def set_password(session: AsyncSession, user: AdminUser, new_password: str) -> None:
    user.hashed_password = hash_password(new_password)
    await session.commit()


# ─── Group helper ─────────────────────────────────────────────────────


async def get_group(session: AsyncSession, group_id: int) -> AdminGroup | None:
    stmt = select(AdminGroup).where(AdminGroup.id == group_id, AdminGroup.deleted_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()
