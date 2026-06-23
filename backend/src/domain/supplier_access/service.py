"""Временный доступ поставщика: создание/список/отзыв supplier-аккаунтов.

Поставщик — это AdminUser с ролью `supplier`, `is_temporary=True` и `expires_at`.
Доступ только к /portal (его собственные батчи). Передача доступа в двух режимах:
  - "password" — сгенерированные логин+пароль (вход через обычный /login)
  - "link"     — magic-ссылка (login_token_hash на юзере, вход без пароля)
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import encrypt_password
from core.security import hash_password
from infrastructure.db.models import AdminRole, AdminUser

DEFAULT_TTL_HOURS = 24 * 7          # 7 дней
MAX_TTL_HOURS = 24 * 90            # 90 дней
SUPPLIER_ROLE = "supplier"


class SupplierAccessError(Exception):
    """Доменная ошибка создания/управления supplier-доступом."""


def _hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


@dataclass
class CreatedSupplierAccess:
    user: AdminUser
    # plaintext, показываются ОДИН раз:
    password: str | None        # режим "password"
    login_token: str | None     # режим "link" (caller строит URL)


async def _supplier_role(session: AsyncSession) -> AdminRole:
    role = (await session.execute(
        select(AdminRole).where(AdminRole.name == SUPPLIER_ROLE)
    )).scalar_one_or_none()
    if role is None:
        raise SupplierAccessError(
            "Роль 'supplier' не найдена — нужен прогон seed (перезапуск app)")
    return role


async def _unique_username(session: AsyncSession) -> str:
    for _ in range(10):
        candidate = f"supplier_{secrets.token_hex(4)}"
        exists = (await session.execute(
            select(AdminUser.id).where(AdminUser.username == candidate)
        )).scalar_one_or_none()
        if exists is None:
            return candidate
    raise SupplierAccessError("Не удалось сгенерировать уникальный логин")


async def create_supplier_access(
    session: AsyncSession,
    *,
    creator: AdminUser,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    note: str | None = None,
    handover: str = "password",
) -> CreatedSupplierAccess:
    """Создать временного поставщика. handover ∈ {"password","link"}."""
    if handover not in ("password", "link"):
        raise SupplierAccessError("handover должен быть 'password' или 'link'")
    ttl_hours = max(1, min(int(ttl_hours), MAX_TTL_HOURS))

    role = await _supplier_role(session)
    username = await _unique_username(session)
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)

    password_plain: str | None = None
    login_token_plain: str | None = None

    # Пароль генерируем ВСЕГДА (даже для link-режима — на случай fallback-входа),
    # но возвращаем только в password-режиме.
    pw = secrets.token_urlsafe(12)
    user = AdminUser(
        username=username,
        email=None,
        hashed_password=hash_password(pw),
        # Обратимо шифруем пароль, чтобы super_admin мог посмотреть его позже в
        # списке (только для временных supplier-аккаунтов; super_admin-only показ).
        temp_password_enc=encrypt_password(pw),
        full_name=(note or "Поставщик доступов")[:255],
        is_active=True,
        is_temporary=True,
        expires_at=expires_at,
    )
    if handover == "password":
        password_plain = pw
    else:  # link
        login_token_plain = secrets.token_urlsafe(32)
        user.login_token_hash = _hash_token(login_token_plain)

    user.roles.append(role)
    session.add(user)
    await session.commit()
    await session.refresh(user, attribute_names=["id", "username", "expires_at"])
    return CreatedSupplierAccess(
        user=user, password=password_plain, login_token=login_token_plain)


async def list_supplier_accesses(session: AsyncSession) -> list[AdminUser]:
    """Все supplier-аккаунты (активные и истёкшие), свежие сверху."""
    rows = (await session.execute(
        select(AdminUser)
        .where(AdminUser.is_temporary.is_(True), AdminUser.deleted_at.is_(None))
        .options(selectinload(AdminUser.roles))
        .order_by(AdminUser.id.desc())
    )).scalars().all()
    return list(rows)


async def revoke_supplier_access(session: AsyncSession, user_id: int) -> bool:
    """Отозвать доступ — деактивировать аккаунт (is_active=False) + убить токен."""
    user = (await session.execute(
        select(AdminUser).where(
            AdminUser.id == user_id,
            AdminUser.is_temporary.is_(True),
            AdminUser.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if user is None:
        return False
    user.is_active = False
    user.login_token_hash = None
    await session.commit()
    return True


async def login_by_magic_token(session: AsyncSession, token: str) -> AdminUser | None:
    """Найти живого поставщика по magic-токену. None если нет/истёк/деактивирован."""
    if not token:
        return None
    h = _hash_token(token)
    user = (await session.execute(
        select(AdminUser)
        .where(AdminUser.login_token_hash == h, AdminUser.deleted_at.is_(None))
        .options(
            selectinload(AdminUser.roles).selectinload(AdminRole.pages),
            selectinload(AdminUser.roles).selectinload(AdminRole.permissions),
            selectinload(AdminUser.group),
            selectinload(AdminUser.direct_pages),
        )
    )).scalar_one_or_none()
    if user is None or not user.is_active or user.is_expired:
        return None
    return user
