"""/admin/api/wp-sites + nested credentials."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import decrypt_password

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.wp_sites import (
    BulkDeleteRequest,
    CreateCredentialRequest,
    CreateSiteRequest,
    ImportResultResponse,
    PoolSummaryResponse,
    SiteAnalyticsResponse,
    UpdateCredentialRequest,
    UpdateSiteRequest,
    WpCredentialResponse,
    WpSiteDetail,
    WpSiteListItem,
    WpSiteListResponse,
)
from api.admin.schemas.wp_batches import ProvisionRequest
from api.common.pagination import DEFAULT_LIMIT, MAX_LIMIT, encode_cursor
from core.db import get_db_read, get_db_write
from domain.wp_sites.service import (
    bulk_delete_credentials_by_filter,
    bulk_soft_delete_credentials,
    count_credentials_by_filter,
    create_credential,
    create_site,
    get_credential,
    get_site,
    import_csv,
    list_credential_tags,
    list_credentials,
    list_sites,
    pool_summary,
    pool_summary_cached,
    site_analytics,
    soft_delete_credential,
    soft_delete_site,
    update_credential,
    update_site,
)
from infrastructure.db.models import AdminUser, WpCredential, WpSite

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/wp-sites", tags=["wp-sites"])


def _decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    import base64
    import json

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        return int(json.loads(raw)["after_id"])
    except Exception:
        return None


def _check_can_view(viewer: AdminUser) -> None:
    if not (viewer.is_super_admin or viewer.is_group_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


_CONFIRMED_VALID_KINDS = ("ok", "manual_valid")
_CONFIRMED_INVALID_KINDS = ("auth_invalid", "permission_denied", "manual_invalid")


def _agg_channel(creds, attr: str) -> bool | None:
    """Агрегат канала по cred сайта: True если хоть один подтвердил (=True),
    False если хоть один опроверг (=False) и никто не подтвердил, иначе None."""
    vals = [getattr(c, attr) for c in creds]
    if any(v is True for v in vals):
        return True
    if any(v is False for v in vals):
        return False
    return None


def _cred_category(c) -> str:
    """one of: 'valid' | 'invalid' | 'pending' | 'transient'.

    Единый источник истины — generated column `cred_status` (миграция 0025).
    БД вычисляет его сама; читаем готовое значение, без пере-вывода логики.
    """
    return c.cred_status


def _site_list_item(site) -> WpSiteListItem:
    live_creds = [c for c in site.credentials if c.deleted_at is None]
    cats = [_cred_category(c) for c in live_creds]
    # max(last_validated_at) — когда сайт последний раз трогали
    last_check = max(
        (c.last_validated_at for c in live_creds if c.last_validated_at is not None),
        default=None,
    )
    # Usage stats — сумма постов по cred-ам + макс. last_used_at
    total_uses = sum((c.amount_use or 0) for c in live_creds)
    last_used = max(
        (c.last_used_at for c in live_creds if c.last_used_at is not None),
        default=None,
    )
    return WpSiteListItem(
        id=site.id,
        domain=site.domain,
        hint_path=site.hint_path,
        hint_port=site.hint_port,
        last_working_url=site.last_working_url,
        last_working_at=site.last_working_at,
        is_active=site.is_active,
        language=site.language,
        language_detected_at=site.language_detected_at,
        note=site.note,
        created_at=site.created_at,
        consecutive_site_failures=site.consecutive_site_failures or 0,
        last_site_failure_at=site.last_site_failure_at,
        last_site_failure_kind=site.last_site_failure_kind,
        auto_disabled_at=site.auto_disabled_at,
        credentials_total=len(live_creds),
        credentials_valid=sum(1 for c in cats if c == "valid"),
        credentials_invalid=sum(1 for c in cats if c == "invalid"),
        credentials_pending=sum(1 for c in cats if c == "pending"),
        credentials_transient=sum(1 for c in cats if c == "transient"),
        credentials_provisioned=sum(1 for c in live_creds if c.provisioned),
        site_can_xmlrpc=_agg_channel(live_creds, "can_xmlrpc"),
        site_can_post_via_xmlrpc=_agg_channel(live_creds, "can_post_via_xmlrpc"),
        site_can_admin=_agg_channel(live_creds, "can_admin_login"),
        last_credential_check_at=last_check,
        total_uses=total_uses,
        last_used_at=last_used,
    )


# ─── Aggregates / lookups ────────────────────────────────────────────


@router.get("/summary", response_model=PoolSummaryResponse)
async def summary_endpoint(
    live: bool = False,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> PoolSummaryResponse:
    """Summary-карточки пула.

    По умолчанию читает materialized view (дёшево — для частого polling).
    `?live=true` — считает напрямую (свежесть во время активной валидации,
    фронт передаёт его когда есть running validation / active batches).
    """
    _check_can_view(viewer)
    data = await (pool_summary(session) if live else pool_summary_cached(session))
    return PoolSummaryResponse(**data)


@router.get("/credential-tags", response_model=list[str])
async def credential_tags_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[str]:
    _check_can_view(viewer)
    from domain.wp_sites.service import effective_allowed_tags
    allowed = await effective_allowed_tags(session, viewer)
    return await list_credential_tags(session, allowed=allowed)


# ─── Sites: CRUD ─────────────────────────────────────────────────────


@router.get("", response_model=WpSiteListResponse)
async def list_sites_endpoint(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    search: str | None = Query(default=None, max_length=200),
    status: str = Query(
        default="all",
        pattern="^(all|active|auto-disabled|off|usable|unusable|cred_valid|cred_invalid|cred_transient)$",
    ),
    sort: str = Query(default="alpha", pattern="^(alpha|recent|valid_desc|transient_desc|most_used)$"),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> WpSiteListResponse:
    _check_can_view(viewer)
    after = _decode_cursor(cursor)
    rows, total = await list_sites(
        session, after_id=after, limit=limit, search=search, status=status, sort=sort,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    items = [_site_list_item(s) for s in rows]
    # Для non-alpha сортов cursor — это offset, для alpha — id.
    next_cursor: str | None = None
    if has_more and rows:
        if sort == "alpha":
            next_cursor = encode_cursor(rows[-1].id)
        else:
            base_offset = (after or 0)
            next_cursor = encode_cursor(base_offset + len(rows))
    return WpSiteListResponse(items=items, next_cursor=next_cursor, has_more=has_more, total=total)


@router.post("", response_model=WpSiteDetail, status_code=status.HTTP_201_CREATED)
async def create_site_endpoint(
    payload: CreateSiteRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> WpSiteDetail:
    try:
        site = await create_site(
            session,
            domain=payload.domain,
            hint_path=payload.hint_path,
            hint_port=payload.hint_port,
            note=payload.note,
        )
    except IntegrityError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e.orig)) from e
    log.info("wp_sites.created", actor_id=actor.id, site_id=site.id, domain=site.domain)
    return WpSiteDetail(
        **{k: getattr(site, k) for k in (
            "id", "domain", "hint_path", "hint_port", "last_working_url",
            "last_working_at", "is_active", "language", "note", "created_at",
        )},
        credentials=[WpCredentialResponse.model_validate(c) for c in site.credentials if c.deleted_at is None],
    )


@router.get("/{site_id}", response_model=WpSiteDetail)
async def get_site_endpoint(
    site_id: int,
    include_password: bool = Query(default=False),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> WpSiteDetail:
    _check_can_view(viewer)
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")

    # Password reveal — только super_admin + явный запрос. Audit-log.
    reveal_passwords = include_password and viewer.is_super_admin
    if include_password and not viewer.is_super_admin:
        log.warning("wp_sites.password_denied", viewer_id=viewer.id, site_id=site_id)
    if reveal_passwords:
        log.warning(
            "wp_sites.passwords_revealed",
            actor_id=viewer.id, site_id=site_id,
            count=len([c for c in site.credentials if c.deleted_at is None]),
        )

    live_creds = [c for c in site.credentials if c.deleted_at is None]
    cred_items: list[WpCredentialResponse] = []
    for c in live_creds:
        item = WpCredentialResponse.model_validate(c)
        if reveal_passwords and c.password:
            try:
                item.password = decrypt_password(c.password)
            except Exception as e:
                log.warning("wp_sites.decrypt_failed", cred_id=c.id, error=str(e))
                item.password = None
        cred_items.append(item)

    return WpSiteDetail(
        **{k: getattr(site, k) for k in (
            "id", "domain", "hint_path", "hint_port", "last_working_url",
            "last_working_at", "is_active", "language", "language_detected_at",
            "note", "created_at", "consecutive_site_failures", "last_site_failure_at",
            "last_site_failure_kind", "auto_disabled_at", "cf_protected", "wp_version",
            "active_theme", "file_editing_disabled", "homepage_is_static_page",
            "homepage_page_id",
        )},
        credentials=cred_items,
    )


@router.get("/{site_id}/analytics", response_model=SiteAnalyticsResponse)
async def get_site_analytics_endpoint(
    site_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> SiteAnalyticsResponse:
    """Аналитика по сайту: счётчики постов + recent posts list."""
    _check_can_view(viewer)
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    data = await site_analytics(session, site_id, recent_limit=50)
    return SiteAnalyticsResponse(domain=site.domain, **data)


@router.get("/{site_id}/events")
async def get_site_events_endpoint(
    site_id: int,
    limit: int = 50,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[dict]:
    """История ошибок по сайту (append-only site_events). Для UI вкладки."""
    _check_can_view(viewer)
    from domain.site_events import list_site_events

    rows = await list_site_events(session, site_id=site_id, limit=min(limit, 200))
    return [
        {
            "id": e.id,
            "source": e.source,
            "error_kind": e.error_kind,
            "error_message": e.error_message,
            "credential_id": e.credential_id,
            "posting_run_id": e.posting_run_id,
            "proxy_id": e.proxy_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


@router.patch("/{site_id}", response_model=WpSiteDetail)
async def update_site_endpoint(
    site_id: int,
    payload: UpdateSiteRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> WpSiteDetail:
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    try:
        site = await update_site(
            session,
            site=site,
            domain=payload.domain,
            hint_path=payload.hint_path if "hint_path" in payload.model_fields_set else ...,
            hint_port=payload.hint_port if "hint_port" in payload.model_fields_set else ...,
            is_active=payload.is_active,
            note=payload.note if "note" in payload.model_fields_set else ...,
        )
    except IntegrityError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e.orig)) from e
    log.info("wp_sites.updated", actor_id=actor.id, site_id=site_id)
    return WpSiteDetail(
        **{k: getattr(site, k) for k in (
            "id", "domain", "hint_path", "hint_port", "last_working_url",
            "last_working_at", "is_active", "language", "note", "created_at",
        )},
        credentials=[WpCredentialResponse.model_validate(c) for c in site.credentials if c.deleted_at is None],
    )


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site_endpoint(
    site_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    await soft_delete_site(session, site)
    log.info("wp_sites.deleted", actor_id=actor.id, site_id=site_id)
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="wp_sites.delete",
        resource_type="wp_site", resource_id=site_id,
        changes={"domain": site.domain},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Credentials: under sites ────────────────────────────────────────


@router.get("/{site_id}/credentials", response_model=list[WpCredentialResponse])
async def list_site_credentials_endpoint(
    site_id: int,
    include_password: bool = Query(default=False),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[WpCredentialResponse]:
    _check_can_view(viewer)
    rows, _ = await list_credentials(session, site_id=site_id, limit=200)
    # Password reveal — только super_admin и только если запросил явно.
    reveal = include_password and viewer.is_super_admin
    if include_password and not viewer.is_super_admin:
        log.warning(
            "wp_sites.credentials.password_denied",
            viewer_id=viewer.id, site_id=site_id,
        )
    if reveal:
        log.warning(
            "wp_sites.credentials.password_revealed",
            actor_id=viewer.id, site_id=site_id, count=len(rows),
        )
    out: list[WpCredentialResponse] = []
    for c in rows:
        item = WpCredentialResponse.model_validate(c)
        if reveal:
            try:
                item.password = decrypt_password(c.password) if c.password else None
            except Exception as e:
                log.warning("wp_sites.decrypt_failed", cred_id=c.id, error=str(e))
                item.password = None
        out.append(item)
    return out


@router.post(
    "/{site_id}/credentials",
    response_model=WpCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential_endpoint(
    site_id: int,
    payload: CreateCredentialRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> WpCredentialResponse:
    if payload.site_id != site_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="site_id mismatch")
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    cred = await create_credential(
        session,
        site_id=site_id,
        login=payload.login,
        password=payload.password,
        tags=payload.tags,
        note=payload.note,
    )
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Login '{payload.login}' already exists for this site",
        )
    log.info("wp_credentials.created", actor_id=actor.id, site_id=site_id, cred_id=cred.id)
    return WpCredentialResponse.model_validate(cred)


@router.patch("/credentials/{cred_id}", response_model=WpCredentialResponse)
async def update_credential_endpoint(
    cred_id: int,
    payload: UpdateCredentialRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> WpCredentialResponse:
    cred = await get_credential(session, cred_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    try:
        cred = await update_credential(
            session,
            cred=cred,
            login=payload.login,
            password=payload.password,
            tags=payload.tags if "tags" in payload.model_fields_set else ...,
            note=payload.note if "note" in payload.model_fields_set else ...,
            is_valid=payload.is_valid,
        )
    except IntegrityError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e.orig)) from e
    log.info("wp_credentials.updated", actor_id=actor.id, cred_id=cred_id)
    return WpCredentialResponse.model_validate(cred)


@router.delete("/credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential_endpoint(
    cred_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    cred = await get_credential(session, cred_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    await soft_delete_credential(session, cred)
    log.info("wp_credentials.deleted", actor_id=actor.id, cred_id=cred_id)
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="wp_credentials.delete",
        resource_type="wp_credential", resource_id=cred_id,
        changes={"login": cred.login, "site_id": cred.site_id},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{site_id}/provision", status_code=status.HTTP_200_OK)
async def provision_site_endpoint(
    site_id: int,
    payload: ProvisionRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Создать наш аккаунт на одном сайте (синхронно — возвращаем результат).
    Требует рабочий admin-доступ с create_users; если наш cred уже есть — skip."""
    site = await get_site(session, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    from domain.wp_provision import provision_site

    res = await provision_site(site_id, role=payload.role, actor_id=actor.id)
    log.info("wp_sites.provision", actor_id=actor.id, site_id=site_id,
             status=res.get("status"), role=payload.role)
    return res


@router.get("/credentials/provision-count")
async def provision_count_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> dict[str, int]:
    """Preview: на скольких сайтах ещё нет нашего cred (но есть admin+create_users)."""
    _check_can_view(viewer)
    from domain.wp_provision import count_provisionable

    n = await count_provisionable(session, batch_id=None)
    return {"provisionable": n}


@router.post("/credentials/bulk-provision", status_code=status.HTTP_202_ACCEPTED)
async def bulk_provision_endpoint(
    payload: ProvisionRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Массовое создание наших аккаунтов на ВСЕХ подходящих сайтах без нашего
    cred (фоновая задача)."""
    from domain.wp_provision import count_provisionable

    n = await count_provisionable(session, batch_id=None)
    from workers.taskiq.cron_tasks import provision_bulk_task

    await provision_bulk_task.kiq(
        role=payload.role, concurrency=payload.concurrency, actor_id=actor.id,
    )
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="wp_credentials.bulk_provision",
        resource_type="wp_credential",
        changes={"role": payload.role, "provisionable": n},
    )
    log.info("wp_credentials.bulk_provision", actor_id=actor.id, role=payload.role,
             provisionable=n)
    return {"ok": True, "role": payload.role, "provisionable": n}


@router.post("/credentials/bulk-delete", status_code=status.HTTP_200_OK)
async def bulk_delete_credentials_endpoint(
    payload: BulkDeleteRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict[str, int]:
    deleted = await bulk_soft_delete_credentials(session, payload.ids)
    log.info("wp_credentials.bulk_deleted", actor_id=actor.id, count=deleted)
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="wp_credentials.bulk_delete",
        resource_type="wp_credential",
        changes={"ids_count": len(payload.ids), "deleted": deleted},
    )
    return {"deleted": deleted}


@router.get("/credentials/bulk-filter-count")
async def bulk_filter_count_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    tag: str | None = None,
    source: str | None = None,
    search: str | None = None,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> dict[str, int]:
    """Preview: сколько cred попадёт под bulk-операцию (для confirm-окна)."""
    _check_can_view(viewer)
    n = await count_credentials_by_filter(
        session, status=status_filter, tag=tag, source=source, search=search
    )
    return {"count": n}


@router.post("/credentials/bulk-delete-by-filter", status_code=status.HTTP_200_OK)
async def bulk_delete_by_filter_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    tag: str | None = None,
    source: str | None = None,
    search: str | None = None,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict[str, int]:
    """Массовое soft-delete cred по фильтру. Audit-logged (необратимая для
    UI операция). Требует хотя бы один фильтр — пустой запрос отклоняем чтобы
    случайно не снести весь пул."""
    if not any([status_filter, tag, source, search]):
        raise HTTPException(
            status_code=400,
            detail="Нужен хотя бы один фильтр (status/tag/source/search), "
                   "иначе будет удалён весь пул",
        )
    deleted = await bulk_delete_credentials_by_filter(
        session, status=status_filter, tag=tag, source=source, search=search
    )
    log.warning(
        "wp_credentials.bulk_deleted_by_filter", actor_id=actor.id,
        status=status_filter, tag=tag, source=source, search=search, count=deleted,
    )
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="wp_credentials.bulk_delete_by_filter",
        resource_type="wp_credential",
        changes={"status": status_filter, "tag": tag, "source": source,
                 "search": search, "deleted": deleted},
    )
    return {"deleted": deleted}


# ─── CSV import ──────────────────────────────────────────────────────


@router.post("/import", response_model=ImportResultResponse)
async def import_endpoint(
    file: UploadFile = File(...),
    tag: str | None = Form(default=None),
    mark_as_valid: bool = Form(default=True),
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ImportResultResponse:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .csv files supported")
    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large (>50 MB)",
        )

    result = await import_csv(
        session, csv_bytes=contents, tag=tag,
        source_filename=file.filename, mark_as_valid=mark_as_valid,
    )
    log.info(
        "wp_sites.imported",
        actor_id=actor.id,
        filename=file.filename,
        imported_credentials=result.imported_credentials,
        sites_created=result.sites_created,
    )
    from domain.audit.service import record as audit_record

    await audit_record(
        session, actor=actor, action="wp_sites.import",
        resource_type="wp_sites",
        changes={
            "filename": file.filename,
            "imported_credentials": result.imported_credentials,
            "sites_created": result.sites_created,
            "tag": tag,
        },
    )
    return ImportResultResponse(
        imported_credentials=result.imported_credentials,
        skipped_duplicate_credentials=result.skipped_duplicate_credentials,
        skipped_invalid_rows=result.skipped_invalid_rows,
        total_rows=result.total_rows,
        sites_created=result.sites_created,
        sites_touched=result.sites_touched,
    )


# ─── Полный экспорт credentials с расшифрованными паролями ──────────
#
# Доступно ТОЛЬКО super_admin. Отдаёт plaintext пароли — фактически делает
# то же что и backup БД + знание ключа. Использование разумное: миграция,
# offline-бэкап, передача доступов клиенту/исполнителю.

_EXPORT_HEADER = [
    "domain",
    "login",
    "password",
    "tag",
    "is_valid",
    "site_active",
    "hint_path",
    "hint_port",
    "amount_use",
    "last_used_at",
    "source_filename",
    "note",
]

_EXPORT_BATCH = 500


def _row_for_export(cred: WpCredential, site: WpSite) -> list:
    return [
        site.domain,
        cred.login,
        decrypt_password(cred.password) if cred.password else "",
        cred.tag or "",
        "true" if cred.is_valid else "false",
        "true" if site.is_active else "false",
        site.hint_path or "",
        str(site.hint_port) if site.hint_port else "",
        cred.amount_use or 0,
        cred.last_used_at.isoformat() if cred.last_used_at else "",
        cred.source_filename or "",
        (cred.note or "").replace("\n", " ")[:1000],
    ]


async def _iter_credentials_for_export(
    session: AsyncSession, include_invalid: bool
) -> AsyncIterator[tuple[WpCredential, WpSite]]:
    """Идём батчами по 500, чтобы не материализовать пул целиком."""
    after_id = 0
    while True:
        stmt = (
            select(WpCredential)
            .where(
                WpCredential.deleted_at.is_(None),
                WpCredential.id > after_id,
            )
            .options(selectinload(WpCredential.site))
            .order_by(WpCredential.id)
            .limit(_EXPORT_BATCH)
        )
        if not include_invalid:
            stmt = stmt.where(WpCredential.is_valid.is_(True))
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return
        for cred in rows:
            site = cred.site
            if site is None or site.deleted_at is not None:
                continue
            yield cred, site
        after_id = rows[-1].id
        if len(rows) < _EXPORT_BATCH:
            return


async def _stream_export_csv(
    session: AsyncSession, include_invalid: bool
) -> AsyncIterator[bytes]:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_HEADER)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    n = 0
    async for cred, site in _iter_credentials_for_export(session, include_invalid):
        writer.writerow([str(c) if c is not None else "" for c in _row_for_export(cred, site)])
        n += 1
        if n % _EXPORT_BATCH == 0:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate()
    if buf.tell() > 0:
        yield buf.getvalue().encode("utf-8")


async def _stream_export_json(
    session: AsyncSession, include_invalid: bool
) -> AsyncIterator[bytes]:
    yield b"[\n"
    first = True
    async for cred, site in _iter_credentials_for_export(session, include_invalid):
        row = _row_for_export(cred, site)
        obj = dict(zip(_EXPORT_HEADER, row, strict=True))
        prefix = b"" if first else b",\n"
        first = False
        yield prefix + json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    yield b"\n]\n"


@router.get("/export")
async def export_credentials(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    include_invalid: bool = Query(
        default=False, description="Включать ли credentials с is_valid=false"
    ),
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> StreamingResponse:
    """
    Полный экспорт credentials с РАСШИФРОВАННЫМИ паролями.

    Доступно только super_admin. Используется для бэкапа/миграции/передачи
    доступов. В audit log запишется (когда сделаем audit log в этапе 3).

    Колонки CSV совместимы с форматом импорта (`domain,login,password,...`),
    дополнительные поля внизу — для аудита.
    """
    log.warning(
        "wp_sites.export.requested",
        actor_id=actor.id,
        format=format,
        include_invalid=include_invalid,
    )
    from domain.audit.service import record as audit_record

    await audit_record(
        session, actor=actor, action="wp_sites.export",
        resource_type="wp_credentials",
        changes={"format": format, "include_invalid": include_invalid},
    )

    if format == "json":
        return StreamingResponse(
            _stream_export_json(session, include_invalid),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="wp-credentials.json"',
            },
        )
    return StreamingResponse(
        _stream_export_csv(session, include_invalid),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="wp-credentials.csv"',
        },
    )


# ─── Валидатор credentials (on-demand + status) ──────────────────────


@router.post("/validate", status_code=status.HTTP_202_ACCEPTED)
async def trigger_validate(
    scope: str = Query(default="all", pattern="^(all|invalid|transient|stale)$"),
    actor: AdminUser = Depends(require_super_admin),
) -> dict:
    """
    On-demand запуск валидации credentials.

    scope:
      - all — все валидные сейчас (хотим перепроверить)
      - invalid — только помеченные is_valid=false (вдруг ожили)
      - stale — последняя валидация > 4ч назад / никогда
    """
    from domain.wp_validation.service import get_state
    from workers.taskiq.cron_tasks import validate_ondemand

    state = await get_state()
    if state.running:
        return {
            "ok": False,
            "running": True,
            "message": "Validation already running",
            "state": state.__dict__,
        }

    await validate_ondemand.kiq(scope=scope, actor_id=actor.id)
    log.info("wp_sites.validate.triggered", actor_id=actor.id, scope=scope)
    return {"ok": True, "running": True, "scope": scope}


@router.get("/validation-status")
async def get_validation_status(
    _: AdminUser = Depends(get_current_user),
) -> dict:
    """Состояние массовой валидации (для UI прогресс-бара)."""
    from domain.wp_validation.service import get_state

    state = await get_state()
    return state.__dict__
