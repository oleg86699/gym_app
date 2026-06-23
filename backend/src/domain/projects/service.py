"""
Project service со scope-фильтром:

- super_admin → видит все projects
- group_admin → проекты юзеров своей группы + projects расшаренные группе
- manager → свои + расшаренные ему + расшаренные его группе

Запись (create/update/delete) подчиняется тем же scope-правилам:
- super_admin → может всё
- group_admin → может управлять проектами своей группы (через user-membership
  или group-share). НЕ может удалить чужой проект, если только не назначен super
- manager → может только свои (owner)
"""

from __future__ import annotations

from datetime import UTC, datetime

from datetime import timedelta

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from infrastructure.db.models import (
    AdminUser,
    PostingRun,
    PostingRunStatus,
    Project,
    ProjectWpUsed,
    TextItem,
    TextItemStatus,
    WpCredential,
    WpSite,
    group_projects,
    user_projects,
)

# ─── Visibility helpers ───────────────────────────────────────────────


def _visible_projects_filter(viewer: AdminUser):
    """
    Возвращает SQL-фильтр (WHERE clause), который ограничивает projects
    тем, что viewer имеет право видеть.
    """
    if viewer.is_super_admin:
        return None  # никакого фильтра, видит всё

    if viewer.is_group_admin and viewer.group_id is not None:
        # Видит:
        # (а) проекты юзеров своей группы (owner_group_id = viewer.group_id)
        # (б) проекты, расшаренные его группе через group_projects
        # (в) свои собственные проекты (на всякий случай)
        # (г) projects расшаренные ему индивидуально
        return or_(
            Project.owner_group_id == viewer.group_id,
            Project.owner_user_id == viewer.id,
            Project.id.in_(
                select(group_projects.c.project_id).where(group_projects.c.group_id == viewer.group_id)
            ),
            Project.id.in_(
                select(user_projects.c.project_id).where(user_projects.c.admin_user_id == viewer.id)
            ),
        )

    # manager (или роль без особых прав):
    # (а) свои проекты
    # (б) расшаренные ему индивидуально
    # (в) расшаренные его группе (если состоит в группе)
    conditions = [
        Project.owner_user_id == viewer.id,
        Project.id.in_(
            select(user_projects.c.project_id).where(user_projects.c.admin_user_id == viewer.id)
        ),
    ]
    if viewer.group_id is not None:
        conditions.append(
            Project.id.in_(
                select(group_projects.c.project_id).where(group_projects.c.group_id == viewer.group_id)
            )
        )
    return or_(*conditions)


def can_view_project(viewer: AdminUser, project: Project) -> bool:
    """Sync проверка: может ли viewer видеть этот проект."""
    if viewer.is_super_admin:
        return True
    if project.owner_user_id == viewer.id:
        return True
    if viewer.is_group_admin and viewer.group_id is not None and project.owner_group_id == viewer.group_id:
        return True
    if any(u.id == viewer.id for u in (project.shared_with_users or [])):
        return True
    if viewer.group_id is not None and any(g.id == viewer.group_id for g in (project.shared_with_groups or [])):
        return True
    return False


def can_manage_project(viewer: AdminUser, project: Project) -> bool:
    """
    Может ли viewer править/удалять/шарить проект.
    - super_admin → да
    - owner → да
    - group_admin своей группы → да, если проект принадлежит его группе
    """
    if viewer.is_super_admin:
        return True
    if project.owner_user_id == viewer.id:
        return True
    if (
        viewer.is_group_admin
        and viewer.group_id is not None
        and project.owner_group_id == viewer.group_id
    ):
        return True
    return False


# ─── Queries ──────────────────────────────────────────────────────────


def _project_load_opts():
    return (
        selectinload(Project.owner),
        selectinload(Project.owner_group),
        selectinload(Project.shared_with_users),
        selectinload(Project.shared_with_groups),
    )


async def list_projects(
    session: AsyncSession,
    *,
    viewer: AdminUser,
    after_id: int | None = None,
    limit: int = 50,
    search: str | None = None,
    owner_id: int | None = None,
) -> list[Project]:
    stmt = (
        select(Project)
        .where(Project.deleted_at.is_(None))
        .options(*_project_load_opts())
        .order_by(Project.id.asc())
        .limit(limit + 1)
    )

    scope_filter = _visible_projects_filter(viewer)
    if scope_filter is not None:
        stmt = stmt.where(scope_filter)

    if after_id:
        stmt = stmt.where(Project.id > after_id)

    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(Project.name.ilike(like))

    if owner_id is not None:
        stmt = stmt.where(Project.owner_user_id == owner_id)

    return list((await session.execute(stmt)).scalars().unique().all())


async def reassign_project_owner(
    session: AsyncSession, *, project: Project, new_owner: AdminUser,
) -> Project:
    """Сменить владельца проекта (super_admin-only действие). Обновляем
    owner_user_id и денорм-кэш owner_group_id (используется в scope-фильтрах).
    Прогоны/домены/креды/шеры привязаны к project_id и переходят автоматически."""
    project.owner_user_id = new_owner.id
    project.owner_group_id = new_owner.group_id
    await session.commit()
    refreshed = await get_project(session, project.id)
    assert refreshed is not None
    return refreshed


async def get_project(session: AsyncSession, project_id: int) -> Project | None:
    stmt = (
        select(Project)
        .where(Project.id == project_id, Project.deleted_at.is_(None))
        .options(*_project_load_opts())
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ─── Mutations ────────────────────────────────────────────────────────


async def create_project(
    session: AsyncSession,
    *,
    owner: AdminUser,
    name: str,
    description: str | None = None,
) -> Project:
    project = Project(
        name=name.strip(),
        description=description,
        owner_user_id=owner.id,
        owner_group_id=owner.group_id,
        is_active=True,
    )
    session.add(project)
    await session.commit()
    refreshed = await get_project(session, project.id)
    assert refreshed is not None
    return refreshed


async def update_project(
    session: AsyncSession,
    *,
    project: Project,
    name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
) -> Project:
    if name is not None:
        project.name = name.strip()
    if description is not None:
        project.description = description
    if is_active is not None:
        project.is_active = is_active
    await session.commit()
    refreshed = await get_project(session, project.id)
    assert refreshed is not None
    return refreshed


async def soft_delete_project(session: AsyncSession, project: Project) -> None:
    project.deleted_at = datetime.now(UTC)
    project.is_active = False
    await session.commit()


async def share_with_users(
    session: AsyncSession,
    *,
    project: Project,
    user_ids: list[int],
) -> Project:
    """Полностью заменяет список индивидуальных share-ов."""
    from sqlalchemy import select as _select

    users = (await session.execute(_select(AdminUser).where(AdminUser.id.in_(user_ids)))).scalars().all()
    project.shared_with_users = list(users)
    await session.commit()
    refreshed = await get_project(session, project.id)
    assert refreshed is not None
    return refreshed


async def share_with_groups(
    session: AsyncSession,
    *,
    project: Project,
    group_ids: list[int],
) -> Project:
    from sqlalchemy import select as _select

    from infrastructure.db.models import AdminGroup

    groups = (await session.execute(_select(AdminGroup).where(AdminGroup.id.in_(group_ids)))).scalars().all()
    project.shared_with_groups = list(groups)
    await session.commit()
    refreshed = await get_project(session, project.id)
    assert refreshed is not None
    return refreshed


# ─── Analytics ────────────────────────────────────────────────────────


# Что считается «активным» run-ом для overview
_ACTIVE_RUN_STATUSES = (
    PostingRunStatus.UNPACKING.value,
    PostingRunStatus.SCHEDULED.value,
    PostingRunStatus.QUEUED.value,
    PostingRunStatus.RUNNING.value,
    PostingRunStatus.PAUSED.value,
)

# Что считается «требует внимания super_admin-а»
_FAILED_RUN_STATUSES = (
    PostingRunStatus.FAILED.value,
    PostingRunStatus.NEED_MORE_ADMINS.value,
    PostingRunStatus.INTERRUPTED.value,
)


async def compute_project_stats(
    session: AsyncSession, project_ids: list[int]
) -> dict[int, dict]:
    """
    Live-метрики per project для overview-таблицы:
      {project_id: {
        active_runs, failed_runs, runs_total,
        posted_total, posted_24h,
        last_posted_at, last_run_at, last_activity_at,
      }}
    Считается двумя aggregate-запросами (по runs и по text_items) — обе таблицы
    индексированы по (project_id, status).
    """
    if not project_ids:
        return {}

    out: dict[int, dict] = {
        pid: {
            "active_runs": 0,
            "failed_runs": 0,
            "runs_total": 0,
            "posted_total": 0,
            "posted_24h": 0,
            "last_posted_at": None,
            "last_run_at": None,
            "last_activity_at": None,
            "available_admins": 0,
            "valid_admins_pool": 0,
        }
        for pid in project_ids
    }

    # 1. PostingRun aggregates
    run_rows = (
        await session.execute(
            select(
                PostingRun.project_id,
                func.count(PostingRun.id).label("total"),
                func.count(PostingRun.id)
                .filter(PostingRun.status.in_(_ACTIVE_RUN_STATUSES))
                .label("active"),
                func.count(PostingRun.id)
                .filter(PostingRun.status.in_(_FAILED_RUN_STATUSES))
                .label("failed"),
                func.max(PostingRun.created_at).label("last_run_at"),
            )
            .where(
                PostingRun.deleted_at.is_(None),
                PostingRun.project_id.in_(project_ids),
            )
            .group_by(PostingRun.project_id)
        )
    ).all()
    for r in run_rows:
        pid = int(r[0])
        out[pid]["runs_total"] = int(r[1] or 0)
        out[pid]["active_runs"] = int(r[2] or 0)
        out[pid]["failed_runs"] = int(r[3] or 0)
        out[pid]["last_run_at"] = r[4]

    # 2. TextItem aggregates (только posted — это «реальные ссылки»)
    threshold_24h = datetime.now(UTC) - timedelta(hours=24)
    item_rows = (
        await session.execute(
            select(
                TextItem.project_id,
                func.count(TextItem.id)
                .filter(TextItem.status == TextItemStatus.POSTED.value)
                .label("posted"),
                func.count(TextItem.id)
                .filter(
                    TextItem.status == TextItemStatus.POSTED.value,
                    TextItem.posted_at >= threshold_24h,
                )
                .label("posted_24h"),
                func.max(
                    case(
                        (TextItem.status == TextItemStatus.POSTED.value, TextItem.posted_at),
                        else_=None,
                    )
                ).label("last_posted_at"),
            )
            .where(TextItem.project_id.in_(project_ids))
            .group_by(TextItem.project_id)
        )
    ).all()
    for r in item_rows:
        pid = int(r[0])
        out[pid]["posted_total"] = int(r[1] or 0)
        out[pid]["posted_24h"] = int(r[2] or 0)
        out[pid]["last_posted_at"] = r[3]

    # 3. last_activity = max(last_posted_at, last_run_at) — что было свежее
    for pid, st in out.items():
        cands = [t for t in (st["last_posted_at"], st["last_run_at"]) if t is not None]
        st["last_activity_at"] = max(cands) if cands else None

    # 4. available_admins per project
    # «Доступно» = СAЙТЫ, на которые проект реально может постить и которые ещё
    # не использованы. Считаем по сайтам (не credentials!) и ровно тем же
    # предикатом, что и воркер в _pick_candidate_sites: активный сайт + ≥1 cred
    # с cred_status='valid' И подтверждённым каналом постинга (xmlrpc|admin).
    #
    # Почему не count(creds is_valid=true): тот счётчик (а) множил по нескольку
    # cred на сайт и (б) ловил transient/pending (is_valid=true, но cred_status≠
    # valid) — давал завышенное число, не совпадающее с реальным пулом постинга.
    #
    # Сайт «использован» = ≥1 запись в project_wp_used (порог 1; per-задача лимит
    # max_posts_per_site здесь не применяется — это общая project-метрика).
    postable_cred_exists = exists().where(
        WpCredential.site_id == WpSite.id,
        WpCredential.deleted_at.is_(None),
        WpCredential.cred_status == "valid",
        or_(
            WpCredential.can_post_via_xmlrpc.is_(True),
            WpCredential.can_post_via_admin.is_(True),
        ),
    )
    # Глобальный пул постабельных сайтов (для контекста «X доступно из Y»).
    pool_total = int(
        (await session.execute(
            select(func.count(WpSite.id)).where(
                WpSite.deleted_at.is_(None),
                WpSite.is_active.is_(True),
                postable_cred_exists,
            )
        )).scalar_one()
    )

    for pid in project_ids:
        exhausted_sub = (
            select(ProjectWpUsed.site_id)
            .where(ProjectWpUsed.project_id == pid)
            .group_by(ProjectWpUsed.site_id)
            .having(func.count() >= 1)
            .subquery()
        )
        cnt = int(
            (await session.execute(
                select(func.count(WpSite.id)).where(
                    WpSite.deleted_at.is_(None),
                    WpSite.is_active.is_(True),
                    postable_cred_exists,
                    ~WpSite.id.in_(select(exhausted_sub.c.site_id)),
                )
            )).scalar_one()
        )
        out[pid]["available_admins"] = cnt
        out[pid]["valid_admins_pool"] = pool_total

    return out
