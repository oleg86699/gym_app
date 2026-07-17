"""/admin/api/supplier-access — управление временными доступами поставщиков.

Только super_admin. Создаёт временного supplier-юзера (видит лишь свой /portal),
показывает учётку/ссылку один раз, позволяет посмотреть список и отозвать.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import require_super_admin
from api.admin.schemas.supplier_access import (
    CreateSupplierAccessRequest,
    RegenerateLinkResponse,
    SupplierAccessCreatedResponse,
    SupplierAccessItem,
    SupplierAccessListResponse,
)
from core.config import settings
from core.crypto import decrypt_password
from core.db import get_db_read, get_db_write
from domain.supplier_access.service import (
    SupplierAccessError,
    create_supplier_access,
    list_supplier_accesses,
    regenerate_supplier_link,
    revoke_supplier_access,
)
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/supplier-access", tags=["supplier-access"])


def _base_url(request: Request) -> str:
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")
    forwarded_host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    scheme = request.headers.get("X-Forwarded-Proto") or request.url.scheme
    if forwarded_host:
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url).rstrip("/")


def _magic_url(base: str, token: str | None) -> str | None:
    return f"{base}/portal-login?token={token}" if token else None


@router.post("", response_model=SupplierAccessCreatedResponse,
             status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: CreateSupplierAccessRequest,
    request: Request,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> SupplierAccessCreatedResponse:
    try:
        created = await create_supplier_access(
            session,
            creator=actor,
            ttl_hours=payload.ttl_hours,
            note=payload.note,
            handover=payload.handover,
            batch_ids=payload.batch_ids,
        )
    except SupplierAccessError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    base = _base_url(request)
    magic_url = _magic_url(base, created.login_token)
    log.info("supplier_access.created", actor_id=actor.id,
             user_id=created.user.id, handover=payload.handover,
             granted_batches=created.granted_batches)
    return SupplierAccessCreatedResponse(
        user_id=created.user.id,
        username=created.user.username,
        expires_at=created.user.expires_at,
        note=payload.note,
        handover=payload.handover,
        password=created.password,
        magic_url=magic_url,
        login_url=f"{base}/login",
        granted_batches=created.granted_batches,
    )


@router.get("", response_model=SupplierAccessListResponse)
async def list_endpoint(
    request: Request,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> SupplierAccessListResponse:
    rows = await list_supplier_accesses(session)
    base = _base_url(request)

    def _dec(enc: str | None) -> str | None:
        if not enc:
            return None
        try:
            return decrypt_password(enc)
        except Exception:
            return None

    return SupplierAccessListResponse(items=[
        SupplierAccessItem(
            user_id=u.id,
            username=u.username,
            note=u.full_name,
            is_active=u.is_active,
            expires_at=u.expires_at,
            is_expired=u.is_expired,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
            handover="link" if u.login_token_hash else "password",
            password=_dec(u.temp_password_enc),
            # Ссылку отдаём, только если токен хранится обратимо (создан после 0061).
            # Старым link-доступам она недоступна → кнопка «Обновить ссылку».
            magic_url=_magic_url(base, _dec(u.login_token_enc)),
        )
        for u in rows
    ])


@router.post("/{user_id}/regenerate-link", response_model=RegenerateLinkResponse)
async def regenerate_link_endpoint(
    user_id: int,
    request: Request,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> RegenerateLinkResponse:
    """Выдать НОВУЮ magic-ссылку доступу (старая перестаёт работать). Для получения
    ссылки к доступам, чей исходный токен невосстановим (создан до 0061), либо
    ротации. Работает и для password-доступа (добавит вход по ссылке)."""
    token = await regenerate_supplier_link(session, user_id)
    if token is None:
        raise HTTPException(status_code=404,
                            detail="Supplier access not found / inactive / expired")
    log.info("supplier_access.link_regenerated", actor_id=actor.id, user_id=user_id)
    return RegenerateLinkResponse(
        user_id=user_id, magic_url=_magic_url(_base_url(request), token) or "")


@router.post("/{user_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_endpoint(
    user_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
):
    ok = await revoke_supplier_access(session, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Supplier access not found")
    log.info("supplier_access.revoked", actor_id=actor.id, user_id=user_id)
