"""Public endpoints (без авторизации) для использования invite-токенов."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import COOKIE_NAME
from api.admin.routes.auth import _serialize_me  # переиспользуем сериализацию
from api.admin.schemas.auth import LoginResponse
from api.admin.schemas.invitations import (
    AcceptInvitationRequest,
    PublicInvitationView,
)
from core.config import settings
from core.db import get_db_read, get_db_write
from core.security import create_access_token
from domain.auth.service import get_user_by_id
from domain.invitations.service import (
    InvitationInvalidError,
    accept_invitation,
    lookup_invitation_by_token,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/api/public/invitations", tags=["public-invitations"])


@router.get("/{token}", response_model=PublicInvitationView)
async def view_invitation(
    token: str,
    session: AsyncSession = Depends(get_db_read),
) -> PublicInvitationView:
    """Получить публичную мета-информацию по invite-токену."""
    try:
        inv = await lookup_invitation_by_token(session, token)
    except InvitationInvalidError as e:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(e)) from e

    return PublicInvitationView(
        group_name=inv.group.name if inv.group else None,
        invited_by_username=inv.created_by.username if inv.created_by else None,
        email=inv.email,
        expires_at=inv.expires_at,
    )


@router.post("/{token}/accept", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def accept_invitation_endpoint(
    token: str,
    payload: AcceptInvitationRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_write),
) -> LoginResponse:
    """Принять invite — создать юзера и сразу залогинить (cookie + token)."""
    try:
        user = await accept_invitation(
            session,
            token,
            username=payload.username,
            password=payload.password,
            email=str(payload.email) if payload.email else None,
            full_name=payload.full_name,
        )
    except InvitationInvalidError as e:
        # 410 для тех, что протухли/использованы; 409 для конфликта username
        msg = str(e)
        if "conflict" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from e
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=msg) from e

    # Подгружаем с relations для serialize_me
    full = await get_user_by_id(session, user.id)
    assert full is not None

    token_jwt = create_access_token(subject=full.username, extra={"user_id": full.id})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token_jwt,
        httponly=True,
        secure=settings.ENVIRONMENT != "dev",
        samesite="lax",
        max_age=settings.JWT_TTL_HOURS * 3600,
        path="/",
    )
    log.info("invitations.accepted", user_id=full.id, username=full.username)

    return LoginResponse(access_token=token_jwt, user=_serialize_me(full))
