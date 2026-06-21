"""
Auth middleware/dependencies:
- get_current_user — извлекает юзера из cookie или Authorization header.
- require_role(name) / require_permission(code) / require_super_admin — фабрики.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db_read
from core.security import decode_access_token
from domain.auth.service import get_user_by_id
from infrastructure.db.models import AdminUser

COOKIE_NAME = "admin_token"

# Песочница временных аккаунтов (поставщик): is_temporary=True пускаем ТОЛЬКО на
# эти префиксы. Всё остальное API — 403, даже если эндпоинт гейтит лишь auth.
# Это defense-in-depth поверх page-access/owner-scoping (эндпоинты с одним
# get_current_user иначе были бы доступны supplier-у).
_TEMP_USER_ALLOWED_PREFIXES = (
    "/admin/api/batches",   # owner-scoped: видит/трогает только свои батчи
    "/admin/api/auth/",
    "/admin/api/system/",
)


def _extract_token(
    cookie_value: str | None,
    auth_header: str | None,
) -> str | None:
    if cookie_value:
        return cookie_value
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return None


async def get_current_user(
    request: Request,
    admin_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_read),
) -> AdminUser:
    """
    Извлечь юзера из cookie `admin_token` или header `Authorization: Bearer ...`.
    Бросает 401 если токен отсутствует/невалидный/юзер удалён/неактивен.
    """
    token = _extract_token(admin_token, authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0

    if user_id <= 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")

    user = await get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    # Временный доступ (поставщик) с истёкшим сроком — больше не пускаем.
    if user.is_expired:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Access expired")

    # Песочница: временный аккаунт ходит только по разрешённым префиксам.
    if user.is_temporary:
        path = request.url.path
        if not any(path.startswith(p) for p in _TEMP_USER_ALLOWED_PREFIXES):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Restricted access")

    # Положим в state для удобства логирования/audit
    request.state.user = user
    return user


def require_role(role_name: str) -> Callable[..., Awaitable[AdminUser]]:
    """Dependency-фабрика: пропускает только если у юзера есть указанная роль."""

    async def checker(user: AdminUser = Depends(get_current_user)) -> AdminUser:
        if role_name not in user.role_names and not user.is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role required: {role_name}",
            )
        return user

    return checker


def require_permission(permission_code: str) -> Callable[..., Awaitable[AdminUser]]:
    """Dependency-фабрика: пропускает только при наличии указанного permission."""

    async def checker(user: AdminUser = Depends(get_current_user)) -> AdminUser:
        if not user.has_permission(permission_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission_code}",
            )
        return user

    return checker


async def require_super_admin(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    if not user.is_super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin only")
    return user


def require_page_access(page_path: str) -> Callable[..., Awaitable[AdminUser]]:
    """Dependency-фабрика: пропускает super_admin или пользователя, у которого
    страница `page_path` доступна (через роль или прямое назначение). Единый
    источник истины с UI-навигацией — тот же accessible_page_paths(). Так
    «открыть доступ руками» (назначить страницу роли/юзеру) работает и для API."""

    async def checker(user: AdminUser = Depends(get_current_user)) -> AdminUser:
        pages = user.accessible_page_paths()  # super_admin → {"*"}
        if "*" in pages or page_path in pages:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Page access required: {page_path}",
        )

    return checker
