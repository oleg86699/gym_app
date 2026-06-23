"""/admin/api/text-items/{id} — просмотр и редактирование одного текста."""

from __future__ import annotations

import hashlib

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user
from api.admin.schemas.postings import TextItemDetailResponse, UpdateTextItemRequest
from api.admin.schemas.project_domains import ResolveTextItemRequest
from core.config import settings
from core.db import get_db_read, get_db_write
from core.storage import StorageError, storage
from domain.postings.service import (
    EDITABLE_TEXT_STATUSES,
    can_manage_run,
    can_view_run,
    get_run,
    get_text_item,
    update_text_item_meta,
)
from domain.projects.service import get_project
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/text-items", tags=["text-items"])


async def _load_item_run_project(session: AsyncSession, item_id: int):
    item = await get_text_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Text item not found")
    run = await get_run(session, item.posting_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    project = await get_project(session, run.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return item, run, project


def _read_content(storage_key: str | None) -> str:
    if not storage_key:
        return ""
    try:
        raw = storage.get_bytes(settings.MINIO_BUCKET_TEXT_ITEMS, storage_key)
    except StorageError as e:
        log.warning("text_items.read_failed", storage_key=storage_key, error=str(e))
        return ""
    return raw.decode("utf-8", errors="replace")


@router.get("/{item_id}", response_model=TextItemDetailResponse)
async def get_text_item_detail(
    item_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> TextItemDetailResponse:
    item, run, project = await _load_item_run_project(session, item_id)
    if not can_view_run(viewer, run, project):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this text"
        )

    from domain.texts import read_item_body
    content = await read_item_body(session, text_id=item.text_id, storage_key=item.storage_key)
    editable = (
        can_manage_run(viewer, run, project)
        and item.status in EDITABLE_TEXT_STATUSES
    )

    return TextItemDetailResponse(
        id=item.id,
        posting_run_id=item.posting_run_id,
        project_id=item.project_id,
        status=item.status,
        title=item.title,
        original_filename=item.original_filename,
        byte_size=item.byte_size,
        attempts=item.attempts,
        last_error=item.last_error,
        posted_url=item.posted_url,
        post_id=item.post_id,
        posted_at=item.posted_at,
        created_at=item.created_at,
        site={"id": item.site.id, "domain": item.site.domain} if item.site else None,
        credential=(
            {"id": item.credential.id, "login": item.credential.login}
            if item.credential
            else None
        ),
        # link-типы + Фаза A (разбор ссылок / язык) — нужны UI (resolve, бейджи)
        link_url=item.link_url,
        link_anchor=item.link_anchor,
        placed_via=item.placed_via,
        verified_at=item.verified_at,
        target_domain=item.target_domain,
        lang=item.lang,
        link_candidates=item.link_candidates,
        content=content,
        editable=editable,
    )


@router.put("/{item_id}", response_model=TextItemDetailResponse)
async def update_text_item(
    item_id: int,
    payload: UpdateTextItemRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> TextItemDetailResponse:
    item, run, project = await _load_item_run_project(session, item_id)
    if not can_manage_run(viewer, run, project):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cannot edit this text"
        )
    if item.status not in EDITABLE_TEXT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot edit text in status '{item.status}'",
        )

    new_bytes = payload.content.encode("utf-8")
    # B1: тело — в texts (источник истины). MinIO дублируем на переходный период
    # (если ещё есть storage_key) — чтобы fallback/бэкап оставались согласованы.
    if item.text_id is not None:
        from domain.texts import update_text_body
        await update_text_body(session, item.text_id, body=payload.content, title=payload.title)
    if item.storage_key:
        try:
            storage.put_bytes(
                settings.MINIO_BUCKET_TEXT_ITEMS,
                item.storage_key,
                new_bytes,
                content_type="text/html; charset=utf-8",
            )
        except StorageError as e:
            log.warning("text_items.minio_dualwrite_failed", item_id=item_id, error=str(e))
    if item.text_id is None and not item.storage_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Нет ни text_id, ни storage_key — некуда сохранять тело",
        )

    content_hash = hashlib.sha256(new_bytes).hexdigest()
    await update_text_item_meta(
        session,
        item_id=item_id,
        title=payload.title,
        byte_size=len(new_bytes),
        content_hash=content_hash,
    )
    log.info(
        "text_items.updated",
        item_id=item_id,
        actor_id=viewer.id,
        bytes=len(new_bytes),
    )

    # Возвращаем свежую версию через GET-логику
    return await get_text_item_detail(item_id, viewer, session)


@router.post("/{item_id}/update-remote", status_code=status.HTTP_200_OK)
async def update_remote_endpoint(
    item_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Перезалить текущий текст в уже опубликованный пост на сайте (перебор
    рабочих доступов к домену: XML-RPC → admin)."""
    item, run, project = await _load_item_run_project(session, item_id)
    if not can_manage_run(viewer, run, project):
        raise HTTPException(status_code=403, detail="Cannot manage this run")
    if item.status != "posted" or not item.post_id:
        raise HTTPException(status_code=409, detail="Текст не опубликован — нечего обновлять")
    from domain.wp_post_ops import update_remote_post

    res = await update_remote_post(item_id, actor_id=viewer.id)
    from domain.audit.service import record as audit_record
    await audit_record(session, actor=viewer, action="text_items.update_remote",
                       resource_type="text_item", resource_id=item_id,
                       changes={"status": res.get("status"), "via": res.get("via")})
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=f"Не удалось обновить: {res.get('status')}")
    return res


@router.post("/{item_id}/delete-remote", status_code=status.HTTP_200_OK)
async def delete_remote_endpoint(
    item_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Удалить опубликованный пост с сайта (перебор рабочих доступов)."""
    item, run, project = await _load_item_run_project(session, item_id)
    if not can_manage_run(viewer, run, project):
        raise HTTPException(status_code=403, detail="Cannot manage this run")
    if not item.post_id:
        raise HTTPException(status_code=409, detail="Нет post_id — нечего удалять")
    from domain.wp_post_ops import delete_remote_post

    res = await delete_remote_post(item_id, actor_id=viewer.id)
    from domain.audit.service import record as audit_record
    await audit_record(session, actor=viewer, action="text_items.delete_remote",
                       resource_type="text_item", resource_id=item_id,
                       changes={"status": res.get("status"), "via": res.get("via")})
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=f"Не удалось удалить: {res.get('status')}")
    return res


@router.post("/{item_id}/resolve", status_code=status.HTTP_200_OK)
async def resolve_text_item(
    item_id: int,
    payload: ResolveTextItemRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Дозаполнить needs_review-задачу: задать целевую ссылку+анкор → pending →
    пере-дёрнуть run (если он уже завершился/в needs_review)."""
    item, run, project = await _load_item_run_project(session, item_id)
    if not can_manage_run(viewer, run, project):
        raise HTTPException(status_code=403, detail="Cannot manage this run")
    if item.status != "needs_review":
        raise HTTPException(status_code=409, detail="Задача не требует дозаполнения")
    from domain.project_domains import resolve_item
    try:
        res = await resolve_item(session, item_id, link=payload.link, anchor=payload.anchor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    from domain.audit.service import record as audit_record
    await audit_record(session, actor=viewer, action="text_items.resolve",
                       resource_type="text_item", resource_id=item_id,
                       changes={"target_domain": res.get("target_domain")})
    return res
