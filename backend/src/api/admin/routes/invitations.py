"""/admin/api/invitations — CRUD пригласительных ссылок."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.invitations import (
    CreateInvitationRequest,
    CreatedInvitationResponse,
    InvitationResponse,
)
from core.config import settings
from core.db import get_db_read, get_db_write
from domain.invitations.service import (
    InvitationScopeError,
    can_manage_invitation,
    create_invitation,
    delete_invitation,
    get_invitation,
    list_invitations,
    revoke_invitation,
)
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/invitations", tags=["invitations"])


def _build_invite_url(request: Request, token: str) -> str:
    """
    Собрать URL вида http(s)://host[:port]/register?token=XXX.
    Приоритет:
      1) settings.PUBLIC_BASE_URL (если задан в .env) — самый надёжный
      2) X-Forwarded-* заголовки от nginx ($http_host сохраняет порт)
      3) request.base_url как fallback
    """
    if settings.PUBLIC_BASE_URL:
        base = settings.PUBLIC_BASE_URL.rstrip("/")
    else:
        forwarded_host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
        scheme = request.headers.get("X-Forwarded-Proto") or request.url.scheme
        base = f"{scheme}://{forwarded_host}" if forwarded_host else str(request.base_url).rstrip("/")
    return f"{base}/register?token={token}"


@router.get("", response_model=list[InvitationResponse])
async def list_invitations_endpoint(
    include_used: bool = True,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[InvitationResponse]:
    """
    super_admin → все
    group_admin → свои + созданные в свою группу
    user → 403
    """
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    rows = await list_invitations(session, viewer=viewer, include_used=include_used)
    return [InvitationResponse.model_validate(i) for i in rows]


@router.post("", response_model=CreatedInvitationResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation_endpoint(
    payload: CreateInvitationRequest,
    request: Request,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> CreatedInvitationResponse:
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    try:
        created = await create_invitation(
            session,
            inviter=viewer,
            group_id=payload.group_id,
            role_ids=payload.role_ids,
            email=str(payload.email) if payload.email else None,
            note=payload.note,
            ttl_hours=payload.ttl_hours,
        )
    except InvitationScopeError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e

    # Перечитать с relations для красивого ответа
    inv_full = await get_invitation(session, created.invitation.id)
    assert inv_full is not None

    invite_url = _build_invite_url(request, created.plain_token)
    log.info(
        "invitations.created",
        actor_id=viewer.id,
        invitation_id=inv_full.id,
        group_id=inv_full.group_id,
        prefix=inv_full.token_prefix,
    )

    base = InvitationResponse.model_validate(inv_full).model_dump()
    return CreatedInvitationResponse(
        **base,
        plain_token=created.plain_token,
        invite_url=invite_url,
    )


@router.post("/{invitation_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation_endpoint(
    invitation_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """Soft revoke: invitation становится недействительным, запись остаётся в БД."""
    inv = await get_invitation(session, invitation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if not await can_manage_invitation(viewer, inv):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot revoke this invitation")
    await revoke_invitation(session, inv)
    log.info("invitations.revoked", actor_id=viewer.id, invitation_id=invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invitation_endpoint(
    invitation_id: int,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """Hard delete: физически удаляет запись из БД. Только super_admin."""
    inv = await get_invitation(session, invitation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    await delete_invitation(session, inv)
    log.info("invitations.deleted", invitation_id=invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
