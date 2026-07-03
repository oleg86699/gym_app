"""/admin/api/projects — CRUD + sharing с учётом scope."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.project_domains import (
    AddProjectDomainRequest,
    AddProjectDomainResult,
    BulkAddDomainsRequest,
    BulkAddDomainsResult,
    DomainAnalyticsRow,
    DomainItemRow,
    DomainItemsResponse,
    DomainPlacementRow,
    DomainRunRow,
    DomainSummaryResponse,
    ProjectDomainResponse,
)
from api.admin.schemas.projects import (
    CreateProjectRequest,
    ProjectListItem,
    ProjectResponse,
    ReassignOwnerRequest,
    ShareGroupsRequest,
    ShareUsersRequest,
    UpdateProjectRequest,
)
from api.common.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PaginatedResponse,
    encode_cursor,
)
from core.db import get_db_read, get_db_write
from domain.projects.service import (
    can_manage_project,
    can_view_project,
    compute_project_stats,
    create_project,
    get_project,
    list_projects,
    purge_project,
    reassign_project_owner,
    restore_project,
    share_with_groups,
    share_with_users,
    soft_delete_project,
    update_project,
)
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


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


@router.get("", response_model=PaginatedResponse[ProjectListItem])
async def list_projects_endpoint(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    search: str | None = Query(default=None, max_length=200),
    owner_id: int | None = Query(default=None, description="Только проекты этого владельца"),
    include_deleted: bool = Query(default=False, description="super_admin: показать soft-deleted"),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> PaginatedResponse[ProjectListItem]:
    after = _decode_cursor(cursor)
    rows = await list_projects(session, viewer=viewer, after_id=after, limit=limit,
                               search=search, owner_id=owner_id, include_deleted=include_deleted)
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    # Live-метрики per project (active runs / posted / failed / last activity)
    stats = await compute_project_stats(session, [p.id for p in rows])
    items: list[ProjectListItem] = []
    for p in rows:
        st = stats.get(p.id, {})
        items.append(ProjectListItem(
            **ProjectResponse.model_validate(p).model_dump(),
            active_runs=st.get("active_runs", 0),
            failed_runs=st.get("failed_runs", 0),
            runs_total=st.get("runs_total", 0),
            posted_total=st.get("posted_total", 0),
            posted_24h=st.get("posted_24h", 0),
            last_activity_at=st.get("last_activity_at"),
            available_admins=st.get("available_admins", 0),
            valid_admins_pool=st.get("valid_admins_pool", 0),
        ))
    next_cursor = encode_cursor(rows[-1].id) if has_more and rows else None
    return PaginatedResponse[ProjectListItem](items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project_endpoint(
    payload: CreateProjectRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProjectResponse:
    project = await create_project(
        session,
        owner=viewer,
        name=payload.name,
        description=payload.description,
    )
    log.info("projects.created", actor_id=viewer.id, project_id=project.id)
    return ProjectResponse.model_validate(project)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project_endpoint(
    project_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> ProjectResponse:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    return ProjectResponse.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project_endpoint(
    project_id: int,
    payload: UpdateProjectRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProjectResponse:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot edit this project")
    updated = await update_project(
        session,
        project=project,
        name=payload.name,
        description=payload.description,
        is_active=payload.is_active,
    )
    log.info("projects.updated", actor_id=viewer.id, project_id=project_id)
    return ProjectResponse.model_validate(updated)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_endpoint(
    project_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete this project")
    await soft_delete_project(session, project, actor_id=viewer.id)
    log.info("projects.deleted", actor_id=viewer.id, project_id=project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_project_endpoint(
    project_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """super_admin: вернуть soft-deleted проект (+ каскадно скрытые runs/domains)."""
    project = await get_project(session, project_id, include_deleted=True)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await restore_project(session, project)
    log.info("projects.restored", actor_id=actor.id, project_id=project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{project_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_project_endpoint(
    project_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """super_admin: полное удаление проекта из БД (каскад: runs → items/used, domains)."""
    project = await get_project(session, project_id, include_deleted=True)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await purge_project(session, project_id)
    log.warning("projects.purged", actor_id=actor.id, project_id=project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _require_share_permission(viewer: AdminUser) -> None:
    """Шарить может только тот, у кого permission projects.share."""
    if not viewer.has_permission("projects.share"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sharing requires 'projects.share' permission (super_admin or group_admin)",
        )


async def _validate_target_users_scope(
    session: AsyncSession, viewer: AdminUser, user_ids: list[int]
) -> None:
    """group_admin может шарить только с юзерами своей группы. super_admin — с любыми."""
    if viewer.is_super_admin or not user_ids:
        return
    from sqlalchemy import select as _select

    rows = (
        await session.execute(_select(AdminUser).where(AdminUser.id.in_(user_ids)))
    ).scalars().all()
    bad = [u.username for u in rows if u.group_id != viewer.group_id]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot share with users outside your group: {', '.join(bad)}",
        )


def _validate_target_groups_scope(viewer: AdminUser, group_ids: list[int]) -> None:
    """group_admin может шарить только со своей группой."""
    if viewer.is_super_admin or not group_ids:
        return
    bad = [gid for gid in group_ids if gid != viewer.group_id]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only share with your own group",
        )


@router.patch("/{project_id}/share/users", response_model=ProjectResponse)
async def share_project_with_users_endpoint(
    project_id: int,
    payload: ShareUsersRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProjectResponse:
    """
    Share с конкретными юзерами доступен любому, кто может управлять проектом
    (owner / group_admin / super_admin). Scope валидации:
    - super_admin → любые user_ids
    - всё остальное → только юзеры своей группы (см. _validate_target_users_scope)
    """
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot manage this project")

    await _validate_target_users_scope(session, viewer, payload.user_ids)

    updated = await share_with_users(session, project=project, user_ids=payload.user_ids)
    log.info("projects.shared_with_users", actor_id=viewer.id, project_id=project_id, user_ids=payload.user_ids)
    return ProjectResponse.model_validate(updated)


@router.patch("/{project_id}/share/groups", response_model=ProjectResponse)
async def share_project_with_groups_endpoint(
    project_id: int,
    payload: ShareGroupsRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProjectResponse:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot manage this project")
    await _require_share_permission(viewer)
    _validate_target_groups_scope(viewer, payload.group_ids)

    updated = await share_with_groups(session, project=project, group_ids=payload.group_ids)
    log.info("projects.shared_with_groups", actor_id=viewer.id, project_id=project_id, group_ids=payload.group_ids)
    return ProjectResponse.model_validate(updated)


@router.post("/{project_id}/reassign-owner", response_model=ProjectResponse)
async def reassign_project_owner_endpoint(
    project_id: int,
    payload: ReassignOwnerRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ProjectResponse:
    """Сменить владельца проекта на другого пользователя. ТОЛЬКО super_admin.

    Сценарий: сотрудник ушёл — проект (со всеми прогонами, доменами, кредами и
    шерами, привязанными к project_id) переходит другому владельцу без потери
    данных. Обновляются owner_user_id + денорм-кэш owner_group_id.
    """
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    from sqlalchemy import select as _select
    new_owner = await session.scalar(
        _select(AdminUser).where(AdminUser.id == payload.new_owner_id,
                                 AdminUser.deleted_at.is_(None)))
    if new_owner is None:
        raise HTTPException(status_code=404, detail="New owner not found")
    old_owner_id = project.owner_user_id
    if old_owner_id == new_owner.id:
        raise HTTPException(status_code=400, detail="Project already owned by this user")
    updated = await reassign_project_owner(session, project=project, new_owner=new_owner)
    log.info("projects.reassigned_owner", actor_id=actor.id, project_id=project_id,
             old_owner_id=old_owner_id, new_owner_id=new_owner.id)
    return ProjectResponse.model_validate(updated)


# ─── Домены проекта (Фаза A: целевые money-домены) ───────────────────

@router.get("/{project_id}/domains", response_model=list[ProjectDomainResponse])
async def list_project_domains(
    project_id: int,
    include_deleted: bool = Query(default=False, description="super_admin: показать soft-deleted"),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[ProjectDomainResponse]:
    inc = include_deleted and viewer.is_super_admin
    project = await get_project(session, project_id, include_deleted=inc)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import list_domains
    rows = await list_domains(session, project_id, include_deleted=inc)
    return [ProjectDomainResponse.model_validate(r) for r in rows]


@router.post("/{project_id}/domains", response_model=AddProjectDomainResult,
             status_code=status.HTTP_201_CREATED)
async def add_project_domain(
    project_id: int,
    payload: AddProjectDomainRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> AddProjectDomainResult:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot manage this project")
    from domain.project_domains import add_domain
    try:
        res = await add_domain(session, project_id, payload.domain)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("projects.domain_added", actor_id=viewer.id, project_id=project_id, **res)
    return AddProjectDomainResult(**res)


@router.post("/{project_id}/domains/bulk", response_model=BulkAddDomainsResult,
             status_code=status.HTTP_201_CREATED)
async def add_project_domains_bulk(
    project_id: int,
    payload: BulkAddDomainsRequest,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> BulkAddDomainsResult:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot manage this project")
    from domain.project_domains import add_domains
    res = await add_domains(session, project_id, payload.domains)
    log.info("projects.domains_bulk_added", actor_id=viewer.id, project_id=project_id,
             added=len(res["added"]), duplicates=len(res["duplicates"]), invalid=len(res["invalid"]))
    return BulkAddDomainsResult(**res)


@router.delete("/{project_id}/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project_domain(
    project_id: int,
    domain_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot manage this project")
    from domain.project_domains import remove_domain
    ok = await remove_domain(session, project_id, domain_id, actor_id=viewer.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Domain not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/domains/{domain_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_project_domain(
    project_id: int,
    domain_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """super_admin: вернуть soft-deleted money-домен."""
    from domain.project_domains import restore_domain
    ok = await restore_domain(session, project_id, domain_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Domain not found")
    log.info("projects.domain_restored", actor_id=actor.id, project_id=project_id, domain_id=domain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{project_id}/domains/{domain_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_project_domain(
    project_id: int,
    domain_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> Response:
    """super_admin: полное (hard) удаление money-домена из БД."""
    from domain.project_domains import purge_domain
    ok = await purge_domain(session, project_id, domain_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Domain not found")
    log.warning("projects.domain_purged", actor_id=actor.id, project_id=project_id, domain_id=domain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/domain-analytics", response_model=list[DomainAnalyticsRow])
async def project_domain_analytics(
    project_id: int,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[DomainAnalyticsRow]:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import domain_analytics
    rows = await domain_analytics(session, project_id)
    return [DomainAnalyticsRow(**r) for r in rows]


@router.get("/{project_id}/domains/{domain}/summary", response_model=DomainSummaryResponse)
async def project_domain_summary(
    project_id: int,
    domain: str,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> DomainSummaryResponse:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import domain_summary
    from domain.wp_sites.service import effective_allowed_tags
    # Пул под домен считаем в рамках тегов, разрешённых смотрящему (tag-access RBAC).
    allowed = await effective_allowed_tags(session, viewer)
    return DomainSummaryResponse(**await domain_summary(
        session, project_id, domain, allowed_tags=allowed))


@router.get("/{project_id}/domains/{domain}/runs", response_model=list[DomainRunRow])
async def project_domain_runs(
    project_id: int,
    domain: str,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[DomainRunRow]:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import domain_runs
    return [DomainRunRow(**r) for r in await domain_runs(session, project_id, domain)]


@router.get("/{project_id}/domains/{domain}/placements", response_model=list[DomainPlacementRow])
async def project_domain_placements(
    project_id: int,
    domain: str,
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[DomainPlacementRow]:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import domain_placements
    return [DomainPlacementRow(**r) for r in await domain_placements(session, project_id, domain)]


@router.get("/{project_id}/domains/{domain}/items", response_model=DomainItemsResponse)
async def project_domain_items(
    project_id: int,
    domain: str,
    cursor: int | None = None,
    limit: int = 50,
    status_filter: str | None = Query(default=None, alias="status"),
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> DomainItemsResponse:
    project = await get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(viewer, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view this project")
    from domain.project_domains import domain_items
    rows = await domain_items(session, project_id, domain,
                              after_id=cursor, status=status_filter, limit=limit)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return DomainItemsResponse(
        items=[DomainItemRow(**r) for r in rows],
        next_cursor=(rows[-1]["id"] if has_more and rows else None),
        has_more=has_more,
    )
