"""Сервис аутентификации: login, password/email change."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.security import hash_password, verify_password
from infrastructure.db.models import AdminRole, AdminUser


def _user_load_options():
    """Стандартный набор selectinload для AdminUser со всеми связями."""
    return (
        selectinload(AdminUser.group),
        selectinload(AdminUser.roles).selectinload(AdminRole.permissions),
        selectinload(AdminUser.roles).selectinload(AdminRole.pages),
        selectinload(AdminUser.direct_pages),
    )


async def get_user_by_username(session: AsyncSession, username: str) -> AdminUser | None:
    stmt = (
        select(AdminUser)
        .where(AdminUser.username == username, AdminUser.deleted_at.is_(None))
        .options(*_user_load_options())
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> AdminUser | None:
    stmt = (
        select(AdminUser)
        .where(AdminUser.id == user_id, AdminUser.deleted_at.is_(None))
        .options(*_user_load_options())
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def authenticate(session: AsyncSession, username: str, password: str) -> AdminUser | None:
    """Найти юзера и сверить пароль. None при любой ошибке (без раскрытия деталей)."""
    user = await get_user_by_username(session, username)
    if user is None or not user.is_active:
        return None
    if user.is_expired:  # временный доступ истёк
        return None
    if not verify_password(password, user.hashed_password):
        return None
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user


async def change_password(session: AsyncSession, user: AdminUser, current: str, new: str) -> bool:
    if not verify_password(current, user.hashed_password):
        return False
    user.hashed_password = hash_password(new)
    await session.commit()
    return True


async def change_email(session: AsyncSession, user: AdminUser, current_password: str, new_email: str) -> bool:
    if not verify_password(current_password, user.hashed_password):
        return False
    user.email = new_email
    await session.commit()
    return True
