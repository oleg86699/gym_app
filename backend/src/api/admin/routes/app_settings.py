"""/admin/api/app-settings — глобальные настройки приложения (super_admin)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.app_settings import (
    AppSettingsResponse,
    UpdateAppSettingsRequest,
)
from core.db import get_db_read, get_db_write
from domain.app_settings.service import _UNSET, get_app_settings, update_app_settings
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/app-settings", tags=["app-settings"])


@router.get("", response_model=AppSettingsResponse)
async def get_settings(
    # Доступно всем авторизованным — UI new-run-формы должен знать дефолты,
    # чтобы корректно показывать предполагаемые значения. Менять — super_admin.
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> AppSettingsResponse:
    row = await get_app_settings(session)
    return AppSettingsResponse.model_validate(row)


@router.put("", response_model=AppSettingsResponse)
async def update_settings(
    payload: UpdateAppSettingsRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> AppSettingsResponse:
    # `default_publish_from`/`_to` нужно отличать "не передали" от "явно null"
    sent = payload.model_fields_set
    pub_from = payload.default_publish_from if "default_publish_from" in sent else _UNSET
    pub_to = payload.default_publish_to if "default_publish_to" in sent else _UNSET

    try:
        row = await update_app_settings(
            session,
            default_concurrency=payload.default_concurrency,
            default_timeout_seconds=payload.default_timeout_seconds,
            global_posting_concurrency=payload.global_posting_concurrency,
            cf_browser_concurrency=payload.cf_browser_concurrency,
            posting_concurrency_floor=payload.posting_concurrency_floor,
            site_disable_threshold=payload.site_disable_threshold,
            site_disable_threshold_cf=payload.site_disable_threshold_cf,
            default_publish_from=pub_from,
            default_publish_to=pub_to,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    log.info(
        "app_settings.updated",
        actor_id=actor.id,
        default_concurrency=row.default_concurrency,
        default_timeout_seconds=row.default_timeout_seconds,
        global_posting_concurrency=row.global_posting_concurrency,
        cf_browser_concurrency=row.cf_browser_concurrency,
        posting_concurrency_floor=row.posting_concurrency_floor,
        site_disable_threshold=row.site_disable_threshold,
        site_disable_threshold_cf=row.site_disable_threshold_cf,
        default_publish_from=str(row.default_publish_from) if row.default_publish_from else None,
        default_publish_to=str(row.default_publish_to) if row.default_publish_to else None,
    )
    from domain.audit.service import record as audit_record

    await audit_record(
        session,
        actor=actor,
        action="app_settings.update",
        resource_type="app_settings",
        resource_id=row.id,
        changes={
            "default_concurrency": row.default_concurrency,
            "default_timeout_seconds": row.default_timeout_seconds,
            "global_posting_concurrency": row.global_posting_concurrency,
            "cf_browser_concurrency": row.cf_browser_concurrency,
            "posting_concurrency_floor": row.posting_concurrency_floor,
            "site_disable_threshold": row.site_disable_threshold,
            "site_disable_threshold_cf": row.site_disable_threshold_cf,
            "default_publish_from": str(row.default_publish_from) if row.default_publish_from else None,
            "default_publish_to": str(row.default_publish_to) if row.default_publish_to else None,
        },
    )
    return AppSettingsResponse.model_validate(row)
