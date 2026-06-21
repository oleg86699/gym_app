"""Auth endpoints: login, logout, me, change-password, change-email."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import COOKIE_NAME, get_current_user
from api.admin.schemas.auth import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    GroupBrief,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RoleBrief,
)
from core.config import settings
from core.db import get_db_read, get_db_write
from core.security import create_access_token
from domain.auth.service import authenticate, change_email, change_password
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ─── Helpers ──────────────────────────────────────────────────────────


def _serialize_me(user: AdminUser) -> MeResponse:
    perms: list[str]
    if user.is_super_admin:
        perms = ["*"]
    else:
        perms = sorted({p.code for r in user.roles if r.is_active for p in r.permissions})

    pages: list[str]
    if user.is_super_admin:
        pages = ["*"]
    else:
        pages = sorted(user.accessible_page_paths())

    return MeResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_super_admin=user.is_super_admin,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        group=GroupBrief.model_validate(user.group) if user.group else None,
        roles=[RoleBrief.model_validate(r) for r in user.roles],
        permissions=perms,
        accessible_pages=pages,
    )


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.ENVIRONMENT != "dev",
        samesite="lax",
        max_age=settings.JWT_TTL_HOURS * 3600,
        path="/",
    )


# ─── Endpoints ────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse, summary="Login by username + password")
async def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db_write),
) -> LoginResponse:
    from domain.audit.service import record as audit_record

    user = await authenticate(session, payload.username, payload.password)
    if user is None:
        log.info("auth.login.failed", username=payload.username)
        await audit_record(
            session,
            actor=None,
            action="auth.login_failed",
            resource_type="user",
            request=request,
            changes={"username": payload.username},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    token = create_access_token(subject=user.username, extra={"user_id": user.id})
    _set_auth_cookie(response, token)
    log.info("auth.login.ok", user_id=user.id, username=user.username)
    await audit_record(
        session,
        actor=user,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    return LoginResponse(access_token=token, user=_serialize_me(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> Response:
    response.delete_cookie(COOKIE_NAME, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeResponse)
async def me(user: AdminUser = Depends(get_current_user)) -> MeResponse:
    return _serialize_me(user)


@router.patch("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    payload: ChangePasswordRequest,
    user: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    # Перевыбираем юзера в writable-сессию (тот, что в Depends — из read-сессии).
    from domain.auth.service import get_user_by_id

    writable_user = await get_user_by_id(session, user.id)
    if writable_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    ok = await change_password(session, writable_user, payload.current_password, payload.new_password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    log.info("auth.password.changed", user_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/me/email", response_model=MeResponse)
async def change_my_email(
    payload: ChangeEmailRequest,
    user: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> MeResponse:
    from domain.auth.service import get_user_by_id

    writable_user = await get_user_by_id(session, user.id)
    if writable_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    ok = await change_email(session, writable_user, payload.current_password, str(payload.new_email))
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    log.info("auth.email.changed", user_id=user.id)

    # Перечитываем со всеми relations
    refreshed = await get_user_by_id(session, user.id)
    assert refreshed is not None
    return _serialize_me(refreshed)
