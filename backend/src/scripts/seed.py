"""
Idempotent seed: системные роли, страницы, permissions + super_admin пользователь.

Запускается автоматически при старте app-контейнера (после миграций).
Безопасно прогонять любое количество раз — ничего не перезапишет.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import WriteSession
from core.logging import configure_logging
from core.security import hash_password
from infrastructure.db.models import (
    AdminPage,
    AdminPermission,
    AdminRole,
    AdminUser,
)

log = structlog.get_logger(__name__)


# ─── Канонические данные системы ──────────────────────────────────────

SYSTEM_ROLES: list[dict[str, str]] = [
    {
        "name": "super_admin",
        "description": "Полный доступ ко всем функциям. Не редактируется.",
    },
    {
        "name": "group_admin",
        "description": "Тимлид. Видит и управляет данными только своей группы.",
    },
    {
        "name": "user",
        "description": "Базовый пользователь. По умолчанию назначается новым.",
    },
    {
        "name": "supplier",
        "description": "Поставщик доступов. Временный аккаунт: грузит и валидирует "
        "ТОЛЬКО свои батчи на портале /portal. Создаётся через /invitations.",
    },
]

# Канонические страницы. При добавлении новой страницы UI — добавь сюда И
# назначь ролям через миграцию (или дай super_admin-у назначить через UI).
SYSTEM_PAGES: list[dict[str, str]] = [
    {"path": "/dashboard", "name": "Dashboard", "description": "Главный экран"},
    {"path": "/profile", "name": "Profile", "description": "Профиль текущего юзера"},
    {"path": "/projects", "name": "Projects", "description": "Проекты"},
    {"path": "/runs", "name": "Runs", "description": "Все прогоны постинга по всем проектам"},
    {"path": "/queue", "name": "Global Queue", "description": "Единая очередь: постинг + ссылки + валидация + лимитер"},
    {"path": "/texts", "name": "Texts", "description": "Библиотека текстов (поиск, reuse)"},
    {"path": "/wp-sites", "name": "WP Sites", "description": "Пул WordPress-сайтов и доступов"},
    {"path": "/batches", "name": "Batches", "description": "Пачки импорта WP-доступов с проверкой и отчётами"},
    {"path": "/proxies", "name": "Proxies", "description": "Пул прокси для постинга"},
    {"path": "/ai-settings", "name": "AI Settings", "description": "AI-провайдеры, модели и шаблоны промптов (генерация контента)"},
    {"path": "/users", "name": "Users", "description": "Управление пользователями"},
    {"path": "/groups", "name": "Groups", "description": "Группы / команды"},
    {"path": "/invitations", "name": "Invitations", "description": "Пригласительные ссылки"},
    {"path": "/roles", "name": "Roles", "description": "Роли и permissions"},
    {"path": "/pages", "name": "Pages", "description": "Матрица доступа к страницам"},
    {"path": "/settings", "name": "Settings", "description": "Глобальные настройки приложения (super_admin)"},
    {"path": "/audit-log", "name": "Audit log", "description": "История ключевых действий (super_admin)"},
]

SYSTEM_PERMISSIONS: list[dict[str, str]] = [
    {"code": "users.view", "resource": "users", "action": "view"},
    {"code": "users.create", "resource": "users", "action": "create"},
    {"code": "users.edit", "resource": "users", "action": "edit"},
    {"code": "users.delete", "resource": "users", "action": "delete"},
    {"code": "users.reset_password", "resource": "users", "action": "reset_password"},
    {"code": "groups.view", "resource": "groups", "action": "view"},
    {"code": "groups.create", "resource": "groups", "action": "create"},
    {"code": "groups.edit", "resource": "groups", "action": "edit"},
    {"code": "groups.delete", "resource": "groups", "action": "delete"},
    {"code": "roles.view", "resource": "roles", "action": "view"},
    {"code": "roles.create", "resource": "roles", "action": "create"},
    {"code": "roles.edit", "resource": "roles", "action": "edit"},
    {"code": "roles.delete", "resource": "roles", "action": "delete"},
    {"code": "pages.view", "resource": "pages", "action": "view"},
    {"code": "pages.assign", "resource": "pages", "action": "assign"},
    {"code": "projects.view", "resource": "projects", "action": "view"},
    {"code": "projects.create", "resource": "projects", "action": "create"},
    {"code": "projects.edit", "resource": "projects", "action": "edit"},
    {"code": "projects.delete", "resource": "projects", "action": "delete"},
    {"code": "projects.share", "resource": "projects", "action": "share"},
    {"code": "invitations.view", "resource": "invitations", "action": "view"},
    {"code": "invitations.create", "resource": "invitations", "action": "create"},
    {"code": "invitations.revoke", "resource": "invitations", "action": "revoke"},
    {"code": "wp_sites.view", "resource": "wp_sites", "action": "view"},
    {"code": "wp_sites.create", "resource": "wp_sites", "action": "create"},
    {"code": "wp_sites.edit", "resource": "wp_sites", "action": "edit"},
    {"code": "wp_sites.delete", "resource": "wp_sites", "action": "delete"},
    {"code": "wp_sites.import", "resource": "wp_sites", "action": "import"},
]

# Какие permissions выдать каким системным ролям.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "super_admin": ["*"],  # волшебная роль, проверка обходится в коде
    "group_admin": [
        "users.view",
        "users.create",
        "users.edit",
        "users.reset_password",
        "roles.view",
        "projects.view",
        "projects.create",
        "projects.edit",
        "projects.share",
        "invitations.view",
        "invitations.create",
        "invitations.revoke",
        # group_admin видит пул (нужно понимать сколько админок доступно), но не редактирует
        "wp_sites.view",
    ],
    "user": [
        # user сам создаёт свои проекты; чужие — через explicit share
        "projects.view",
        "projects.create",
        "projects.edit",
    ],
}

# Какие страницы открыть какой системной роли по умолчанию.
# group_admin намеренно НЕ имеет /groups (групп-management только у super_admin),
# /pages (матрица доступа page × role) и /batches (импорт-пачки WP-доступов).
# /texts (библиотека текстов) тоже super_admin only — при необходимости
# открывают руками через /pages. Всё перечисленное — super_admin only.
ROLE_PAGES: dict[str, list[str]] = {
    "super_admin": [p["path"] for p in SYSTEM_PAGES],
    "group_admin": ["/dashboard", "/profile", "/projects", "/runs", "/queue", "/users", "/roles", "/invitations", "/wp-sites", "/proxies"],
    "user": ["/dashboard", "/profile", "/projects", "/runs", "/queue"],
    # supplier: ТОЛЬКО /batches (owner-scoped — видит лишь свои батчи). Всё
    # остальное закрыто (page-access + песочница _TEMP_USER_ALLOWED_PREFIXES).
    "supplier": ["/batches"],
}


# ─── Хелперы ──────────────────────────────────────────────────────────


async def _ensure_role(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    is_assignable_by_group_admin: bool = False,
) -> AdminRole:
    role = (await session.execute(select(AdminRole).where(AdminRole.name == name))).scalar_one_or_none()
    if role is None:
        role = AdminRole(
            name=name,
            description=description,
            is_active=True,
            is_system=True,
            is_assignable_by_group_admin=is_assignable_by_group_admin,
        )
        session.add(role)
        await session.flush()
        log.info("seed.role.created", name=name)
    return role


async def _ensure_permission(session: AsyncSession, *, code: str, resource: str, action: str) -> AdminPermission:
    perm = (
        await session.execute(select(AdminPermission).where(AdminPermission.code == code))
    ).scalar_one_or_none()
    if perm is None:
        perm = AdminPermission(code=code, resource=resource, action=action)
        session.add(perm)
        await session.flush()
        log.info("seed.permission.created", code=code)
    return perm


async def _ensure_page(session: AsyncSession, *, path: str, name: str, description: str) -> AdminPage:
    page = (await session.execute(select(AdminPage).where(AdminPage.path == path))).scalar_one_or_none()
    if page is None:
        page = AdminPage(path=path, name=name, description=description, is_active=True)
        session.add(page)
        await session.flush()
        log.info("seed.page.created", path=path)
    return page


async def _seed() -> None:
    async with WriteSession() as session:
        # Permissions
        permissions: dict[str, AdminPermission] = {}
        for p in SYSTEM_PERMISSIONS:
            permissions[p["code"]] = await _ensure_permission(
                session, code=p["code"], resource=p["resource"], action=p["action"]
            )

        # Pages
        pages: dict[str, AdminPage] = {}
        for p in SYSTEM_PAGES:
            pages[p["path"]] = await _ensure_page(
                session, path=p["path"], name=p["name"], description=p["description"]
            )

        # Roles + permissions + pages
        from sqlalchemy.orm import selectinload

        # Только роль 'user' по умолчанию делегируется group_admin-у
        assignable_by_group_admin = {"user"}

        roles: dict[str, AdminRole] = {}
        for r in SYSTEM_ROLES:
            role = await _ensure_role(
                session,
                name=r["name"],
                description=r["description"],
                is_assignable_by_group_admin=r["name"] in assignable_by_group_admin,
            )
            # Перечитываем со связями (selectinload) для безопасного апдейта
            role = (
                await session.execute(
                    select(AdminRole)
                    .where(AdminRole.id == role.id)
                    .options(selectinload(AdminRole.permissions), selectinload(AdminRole.pages))
                )
            ).scalar_one()
            roles[r["name"]] = role

            wanted_perm_codes = ROLE_PERMISSIONS.get(r["name"], [])
            if wanted_perm_codes == ["*"]:
                # super_admin — не пишем огромный список, обработаем в коде через has_permission.
                # Но дадим хотя бы маркер: добавим запись с code='*' если ещё нет.
                star = (
                    await session.execute(select(AdminPermission).where(AdminPermission.code == "*"))
                ).scalar_one_or_none()
                if star is None:
                    star = AdminPermission(code="*", resource="*", action="*", description="superuser wildcard")
                    session.add(star)
                    await session.flush()
                if star not in role.permissions:
                    role.permissions.append(star)
            else:
                for code in wanted_perm_codes:
                    perm = permissions[code]
                    if perm not in role.permissions:
                        role.permissions.append(perm)

            for path in ROLE_PAGES.get(r["name"], []):
                page = pages[path]
                if page not in role.pages:
                    role.pages.append(page)

        # Super admin user — создание или загрузка существующего с relations
        super_role = roles["super_admin"]
        admin = (
            await session.execute(
                select(AdminUser)
                .where(AdminUser.username == settings.SUPER_ADMIN_USERNAME)
                .options(selectinload(AdminUser.roles))
            )
        ).scalar_one_or_none()

        if admin is None:
            admin = AdminUser(
                username=settings.SUPER_ADMIN_USERNAME,
                email=settings.SUPER_ADMIN_EMAIL or None,
                hashed_password=hash_password(settings.SUPER_ADMIN_PASSWORD),
                full_name="Super Admin",
                is_active=True,
                roles=[super_role],  # сразу при создании, без lazy load потом
            )
            session.add(admin)
            await session.flush()
            log.info("seed.super_admin.created", username=admin.username)
        else:
            if super_role not in admin.roles:
                admin.roles.append(super_role)
                log.info("seed.super_admin.role_attached")
            else:
                log.info("seed.super_admin.already_exists", username=admin.username)

        await session.commit()
        log.info("seed.done")


def _ensure_minio_buckets() -> None:
    """Создать дефолтные бакеты MinIO (idempotent)."""
    try:
        from core.storage import storage

        storage.ensure_buckets()
        log.info("seed.minio.buckets_ready")
    except Exception as e:
        # Не падаем — стек может стартовать и без MinIO (раннее dev состояние)
        log.warning("seed.minio.skip", error=str(e))


def main() -> None:
    configure_logging(level=settings.LOG_LEVEL, json=settings.ENVIRONMENT != "dev")
    try:
        asyncio.run(_seed())
        _ensure_minio_buckets()
    except Exception as e:
        log.exception("seed.failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
