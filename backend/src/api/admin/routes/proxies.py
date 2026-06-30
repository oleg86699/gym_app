"""/admin/api/proxies — управление proxy-пулом (super_admin)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.proxies import (
    BulkAddRequest,
    BulkAddResult,
    CheckResult,
    CreateProxyRequest,
    ImportRequest,
    ImportResult,
    PoolStats,
    ProviderStat,
    ProxyListResponse,
    ProxyResponse,
    SourceMetadata,
)
from api.common.pagination import CursorParams, encode_cursor
from core.db import get_db_read, get_db_write
from domain.audit.service import record as audit_record
from domain.proxies.service import (
    bulk_create,
    check_proxy,
    create_manual,
    delete_by_source,
    delete_proxy,
    get_proxy,
    import_from_source,
    list_proxies,
    parse_bulk,
    pool_stats,
    provider_counts,
    recheck_all_proxies,
)
from infrastructure.db.models import AdminUser
from infrastructure.proxy_sources import list_source_metadata

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/proxies", tags=["proxies"])


# ─── List + provider stats + source metadata ────────────────────────


@router.get("", response_model=ProxyListResponse)
async def list_endpoint(
    search: str | None = Query(default=None, max_length=200),
    provider: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=200),
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> ProxyListResponse:
    after_id = CursorParams(cursor=cursor).after_id()
    rows = await list_proxies(
        session, search=search, provider=provider, status=status_filter,
        after_id=after_id, limit=limit,
    )
    # list_proxies берёт limit+1 — лишний элемент сигналит «есть ещё»; обрезаем.
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].id) if has_more and rows else None
    return ProxyListResponse(
        items=[ProxyResponse.model_validate(p) for p in rows],
        total=len(rows),
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/providers", response_model=list[ProviderStat])
async def list_providers(
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[ProviderStat]:
    counts = await provider_counts(session)
    return [ProviderStat(source=s, count=c) for s, c in counts.items()]


@router.get("/pools", response_model=PoolStats)
async def get_pool_stats(
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> PoolStats:
    """
    Сводка active+working прокси для выбора в New run dialog.
    Используется UI чтобы построить dropdown с pool-options:
      - direct
      - all (N active)
      - provider:webshare (N)
      - provider:decodo (N)
      - single:<id>  (отдельные прокси, для отладки)
    """
    return PoolStats(**(await pool_stats(session)))


@router.get("/sources", response_model=list[SourceMetadata])
async def list_sources_endpoint(
    _: AdminUser = Depends(require_super_admin),
) -> list[SourceMetadata]:
    return [SourceMetadata.model_validate(s) for s in list_source_metadata()]


# ─── Single CRUD ────────────────────────────────────────────────────


@router.post("", response_model=ProxyResponse, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: CreateProxyRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ProxyResponse:
    p = await create_manual(session, **payload.model_dump())
    log.info("proxies.created", actor_id=actor.id, proxy_id=p.id)
    await audit_record(
        session, actor=actor, action="proxies.create",
        resource_type="proxy", resource_id=p.id,
        changes={"host": p.host, "port": p.port, "provider": p.provider},
    )
    return ProxyResponse.model_validate(p)


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    proxy_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
):
    p = await get_proxy(session, proxy_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    await delete_proxy(session, proxy_id)
    log.info("proxies.deleted", actor_id=actor.id, proxy_id=proxy_id)
    await audit_record(session, actor=actor, action="proxies.delete", resource_type="proxy", resource_id=proxy_id)


@router.post("/{proxy_id}/check", response_model=CheckResult)
async def check_endpoint(
    proxy_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> CheckResult:
    result = await check_proxy(session, proxy_id)
    log.info("proxies.checked", actor_id=actor.id, proxy_id=proxy_id, ok=result.get("ok"))
    return CheckResult.model_validate(result)


@router.post("/recheck-all")
async def recheck_all_endpoint(
    only_active: bool = False,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Health-recheck всего пула (или только active). Оживших разлочивает,
    мёртвых помечает down. Тяжёлая операция — выполняется синхронно с
    concurrency-лимитом; для больших пулов лучше дождаться daily cron."""
    res = await recheck_all_proxies(session, only_active=only_active)
    log.info("proxies.recheck_all", actor_id=actor.id, **res)
    return {"ok": True, **res}


# ─── Bulk add ───────────────────────────────────────────────────────


@router.post("/bulk", response_model=BulkAddResult)
async def bulk_add_endpoint(
    payload: BulkAddRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> BulkAddResult:
    parsed_rows, invalid = parse_bulk(payload.text)
    inserted = await bulk_create(session, parsed_rows)
    log.info("proxies.bulk", actor_id=actor.id, parsed=len(parsed_rows), inserted=inserted)
    return BulkAddResult(parsed=len(parsed_rows), inserted=inserted, invalid=invalid)


# ─── Import from provider ───────────────────────────────────────────


@router.post("/import/{source}", response_model=ImportResult)
async def import_endpoint(
    source: str,
    payload: ImportRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ImportResult:
    try:
        res = await import_from_source(session, source, payload.opts)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    log.info("proxies.import", actor_id=actor.id, source=source, **res)
    await audit_record(
        session, actor=actor, action="proxies.import",
        resource_type="proxy_source", changes={"source": source, **res},
    )
    return ImportResult(**res)


@router.delete("/source/{source}")
async def remove_source_endpoint(
    source: str,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> dict:
    """Удалить все proxy этого source. Полезно перед re-import-ом другим аккаунтом."""
    deleted = await delete_by_source(session, source)
    log.info("proxies.source.removed", actor_id=actor.id, source=source, deleted=deleted)
    await audit_record(
        session, actor=actor, action="proxies.source_remove",
        resource_type="proxy_source", changes={"source": source, "deleted": deleted},
    )
    return {"deleted": deleted}
