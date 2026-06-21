"""Public endpoint для входа поставщика по magic-ссылке (без пароля)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import COOKIE_NAME
from api.admin.routes.auth import _serialize_me
from api.admin.schemas.auth import LoginResponse
from core.config import settings
from core.security import create_access_token
from core.db import get_db_write
from domain.supplier_access.service import login_by_magic_token

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/api/public/portal", tags=["public-portal"])


@router.post("/login", response_model=LoginResponse)
async def portal_magic_login(
    token: str,
    response: Response,
    session: AsyncSession = Depends(get_db_write),
) -> LoginResponse:
    """Вход поставщика по magic-токену — выдаём cookie + JWT как при /login.
    Сессия живёт min(JWT TTL, до expires_at юзера) — get_current_user отрубит
    по истечении срока в любом случае."""
    user = await login_by_magic_token(session, token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_410_GONE,
                            detail="Ссылка недействительна или истекла")
    user.last_login_at = datetime.now(UTC)
    await session.commit()

    token_jwt = create_access_token(subject=user.username, extra={"user_id": user.id})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token_jwt,
        httponly=True,
        secure=settings.ENVIRONMENT != "dev",
        samesite="lax",
        max_age=settings.JWT_TTL_HOURS * 3600,
        path="/",
    )
    log.info("portal.magic_login", user_id=user.id, username=user.username)
    return LoginResponse(access_token=token_jwt, user=_serialize_me(user))
