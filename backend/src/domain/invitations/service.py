"""
Invitations service: создание/листинг/отзыв инвайтов, валидация и принятие.

Безопасность:
- generate_invitation_token() возвращает (plain_token, hash); plain отдаём один раз.
- accept() проверяет hash, срок, отзыв, использование — атомарно помечает used_at.
- Scope для group_admin: может звать только в свою группу, только assignable роли.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.security import hash_password
from infrastructure.db.models import AdminGroup, AdminRole, AdminUser, Invitation


DEFAULT_TTL_HOURS = 12  # короткая ссылка — пол-дня, нужно зарегистрироваться сегодня


@dataclass
class CreatedInvitation:
    invitation: Invitation
    plain_token: str  # отдать клиенту один раз, потом нельзя получить


def _hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _generate_token() -> tuple[str, str, str]:
    """Возвращает (plain_token, hash, prefix)."""
    plain = secrets.token_urlsafe(32)  # 256 bits
    return plain, _hash_token(plain), plain[:8]


# ─── Validation для роли group_admin при создании ────────────────────


class InvitationScopeError(Exception):
    pass


def validate_inviter_scope(
    inviter: AdminUser,
    group_id: int | None,
    target_roles: list[AdminRole],
) -> None:
    """group_admin может звать только в свою группу и только assignable роли."""
    if inviter.is_super_admin:
        return

    # group_admin путь
    if inviter.is_group_admin:
        if group_id is None or group_id != inviter.group_id:
            raise InvitationScopeError("group_admin can only invite into own group")
        bad_roles = [r.name for r in target_roles if not r.is_assignable_by_group_admin]
        if bad_roles:
            raise InvitationScopeError(
                f"group_admin cannot assign these roles: {', '.join(bad_roles)}"
            )
        return

    raise InvitationScopeError("Insufficient permissions to create invitations")


# ─── Create / List / Revoke ──────────────────────────────────────────


async def create_invitation(
    session: AsyncSession,
    *,
    inviter: AdminUser,
    group_id: int | None,
    role_ids: list[int],
    email: str | None,
    note: str | None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> CreatedInvitation:
    # Подгрузить роли
    target_roles: list[AdminRole] = []
    if role_ids:
        target_roles = list(
            (await session.execute(select(AdminRole).where(AdminRole.id.in_(role_ids)))).scalars().all()
        )

    # Роль supplier — только через «Доступы поставщиков» (временные аккаунты),
    # не через обычные приглашения (иначе создаётся постоянный supplier-юзер).
    if any(r.name == "supplier" for r in target_roles):
        raise InvitationScopeError(
            "Роль 'supplier' выдаётся только через «Доступы поставщиков», не приглашением.")

    validate_inviter_scope(inviter, group_id, target_roles)

    plain, h, prefix = _generate_token()
    inv = Invitation(
        token_hash=h,
        token_prefix=prefix,
        created_by_user_id=inviter.id,
        group_id=group_id,
        role_ids=role_ids,
        email=email,
        note=note,
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
    )
    session.add(inv)
    await session.commit()
    return CreatedInvitation(invitation=inv, plain_token=plain)


def _invite_load_opts():
    return (
        selectinload(Invitation.created_by),
        selectinload(Invitation.group),
        selectinload(Invitation.used_by),
    )


async def list_invitations(
    session: AsyncSession,
    *,
    viewer: AdminUser,
    include_used: bool = True,
) -> list[Invitation]:
    stmt = select(Invitation).options(*_invite_load_opts()).order_by(Invitation.created_at.desc())
    if not viewer.is_super_admin:
        # group_admin видит только свои + те, что в его группу
        if viewer.is_group_admin and viewer.group_id is not None:
            stmt = stmt.where(
                (Invitation.group_id == viewer.group_id) | (Invitation.created_by_user_id == viewer.id)
            )
        else:
            return []
    if not include_used:
        stmt = stmt.where(Invitation.used_at.is_(None), Invitation.is_revoked.is_(False))
    return list((await session.execute(stmt)).scalars().all())


async def get_invitation(session: AsyncSession, invitation_id: int) -> Invitation | None:
    stmt = select(Invitation).where(Invitation.id == invitation_id).options(*_invite_load_opts())
    return (await session.execute(stmt)).scalar_one_or_none()


async def revoke_invitation(session: AsyncSession, inv: Invitation) -> None:
    """Soft revoke — помечаем как invalid, запись остаётся для аудита."""
    inv.is_revoked = True
    await session.commit()


async def delete_invitation(session: AsyncSession, inv: Invitation) -> None:
    """Hard delete — физически удаляет строку. Для super_admin при чистке."""
    await session.delete(inv)
    await session.commit()


# ─── Public lookup / accept ──────────────────────────────────────────


class InvitationInvalidError(Exception):
    """Токен битый, истёкший, отозванный или уже использован."""


async def lookup_invitation_by_token(session: AsyncSession, plain_token: str) -> Invitation:
    """Найти invite по plain-токену и проверить, что им можно воспользоваться."""
    h = _hash_token(plain_token)
    inv = (
        await session.execute(
            select(Invitation).where(Invitation.token_hash == h).options(*_invite_load_opts())
        )
    ).scalar_one_or_none()
    if inv is None:
        raise InvitationInvalidError("Invitation not found")
    if inv.is_revoked:
        raise InvitationInvalidError("Invitation revoked")
    if inv.used_at is not None:
        raise InvitationInvalidError("Invitation already used")
    if inv.expires_at < datetime.now(UTC):
        raise InvitationInvalidError("Invitation expired")
    return inv


async def accept_invitation(
    session: AsyncSession,
    plain_token: str,
    *,
    username: str,
    password: str,
    email: str | None,
    full_name: str | None,
) -> AdminUser:
    """Принять invite: создать юзера и пометить invite использованным."""
    inv = await lookup_invitation_by_token(session, plain_token)

    # Создание юзера
    role_ids = list(inv.role_ids or [])
    if not role_ids:
        # дефолтная 'user'
        default = (
            await session.execute(select(AdminRole).where(AdminRole.name == "user"))
        ).scalar_one_or_none()
        if default is not None:
            role_ids = [default.id]

    roles: list[AdminRole] = []
    if role_ids:
        roles = list(
            (await session.execute(select(AdminRole).where(AdminRole.id.in_(role_ids)))).scalars().all()
        )

    user = AdminUser(
        username=username.strip(),
        email=(email.strip() if email else inv.email),
        hashed_password=hash_password(password),
        full_name=full_name,
        group_id=inv.group_id,
        is_active=True,
        roles=roles,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        raise InvitationInvalidError(f"User conflict: {e.orig}") from e

    inv.used_at = datetime.now(UTC)
    inv.used_by_user_id = user.id
    await session.commit()

    return user


async def can_manage_invitation(viewer: AdminUser, inv: Invitation) -> bool:
    if viewer.is_super_admin:
        return True
    if inv.created_by_user_id == viewer.id:
        return True
    if viewer.is_group_admin and viewer.group_id is not None and inv.group_id == viewer.group_id:
        return True
    return False


# ─── Group lookup helper ─────────────────────────────────────────────


async def get_group(session: AsyncSession, group_id: int) -> AdminGroup | None:
    return (
        await session.execute(
            select(AdminGroup).where(AdminGroup.id == group_id, AdminGroup.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
