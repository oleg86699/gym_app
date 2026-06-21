"""
Сводка для /dashboard. Scope-aware:

- super_admin → видит весь стек
- остальные → только свой scope (через _visible_projects_filter)

Возвращает структуру для одного запроса (карточки + списки), чтобы UI не
дёргал 6 endpoint-ов.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domain.projects.service import _visible_projects_filter
from infrastructure.db.models import (
    AdminUser,
    PostingRun,
    PostingRunStatus,
    Project,
    TextItem,
    TextItemStatus,
    WpCredential,
    WpSite,
)

# Активные статусы run-а (показываем как «in flight»)
ACTIVE_RUN_STATUSES = (
    PostingRunStatus.UNPACKING.value,
    PostingRunStatus.QUEUED.value,
    PostingRunStatus.RUNNING.value,
    PostingRunStatus.PAUSED.value,
    PostingRunStatus.SCHEDULED.value,
)

# Финализированные — для recent activity
FINISHED_RUN_STATUSES = (
    PostingRunStatus.DONE.value,
    PostingRunStatus.FAILED.value,
    PostingRunStatus.CANCELLED.value,
    PostingRunStatus.INTERRUPTED.value,
    PostingRunStatus.NEED_MORE_ADMINS.value,
)


def _viewer_scope_filter(viewer: AdminUser):
    """Возвращает WHERE clause на PostingRun по проекту, или None если scope=all."""
    proj_filter = _visible_projects_filter(viewer)
    if proj_filter is None:
        return None
    return PostingRun.project_id.in_(select(Project.id).where(proj_filter))


async def get_dashboard(session: AsyncSession, viewer: AdminUser) -> dict:
    scope = _viewer_scope_filter(viewer)

    # ─── Карточки: активные run-ы / pending texts / posts today / failed today
    active_runs_q = select(func.count(PostingRun.id)).where(
        PostingRun.deleted_at.is_(None),
        PostingRun.status.in_(ACTIVE_RUN_STATUSES),
    )
    if scope is not None:
        active_runs_q = active_runs_q.where(scope)
    active_runs_count = int((await session.execute(active_runs_q)).scalar_one())

    # Pending + posting текстов в активных run-ах
    pending_q = (
        select(func.count(TextItem.id))
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .where(
            PostingRun.deleted_at.is_(None),
            PostingRun.status.in_(ACTIVE_RUN_STATUSES),
            TextItem.status.in_(
                (TextItemStatus.PENDING.value, TextItemStatus.POSTING.value)
            ),
        )
    )
    if scope is not None:
        pending_q = pending_q.where(scope)
    pending_texts_count = int((await session.execute(pending_q)).scalar_one())

    # Posts today (по posted_at, в течение последних 24ч) — в пределах scope
    since_24h = datetime.now(UTC) - timedelta(hours=24)
    posts_today_q = (
        select(func.count(TextItem.id))
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .where(
            PostingRun.deleted_at.is_(None),
            TextItem.status == TextItemStatus.POSTED.value,
            TextItem.posted_at.is_not(None),
            TextItem.posted_at >= since_24h,
        )
    )
    failed_today_q = (
        select(func.count(TextItem.id))
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .where(
            PostingRun.deleted_at.is_(None),
            TextItem.status == TextItemStatus.FAILED.value,
            TextItem.updated_at >= since_24h,
        )
    )
    if scope is not None:
        posts_today_q = posts_today_q.where(scope)
        failed_today_q = failed_today_q.where(scope)
    posts_today = int((await session.execute(posts_today_q)).scalar_one())
    failed_today = int((await session.execute(failed_today_q)).scalar_one())

    # WP-пул — глобально (он не scope-aware, доступен всем кто видит run-ы).
    # sites_usable = операционная метрика «готовы к постингу»: домен жив И есть
    # хотя бы один cred со cred_status='valid'. Согласовано с /wp-sites.
    sites_active = int(
        (
            await session.execute(
                select(func.count(WpSite.id)).where(
                    WpSite.deleted_at.is_(None),
                    WpSite.is_active.is_(True),
                    exists().where(
                        WpCredential.site_id == WpSite.id,
                        WpCredential.deleted_at.is_(None),
                        WpCredential.cred_status == "valid",
                    ),
                )
            )
        ).scalar_one()
    )
    # cred_valid — из единого источника cred_status (миграция 0025), а не
    # сырого is_valid: иначе сюда попадают transient (is_valid=True, но ни один
    # канал не подтвердил) и цифра расходится с /wp-sites.
    cred_valid = int(
        (
            await session.execute(
                select(func.count(WpCredential.id)).where(
                    WpCredential.deleted_at.is_(None),
                    WpCredential.cred_status == "valid",
                )
            )
        ).scalar_one()
    )

    # ─── Списки: active runs (top 10) + recent finished (top 10)
    active_list_q = (
        select(PostingRun)
        .where(
            PostingRun.deleted_at.is_(None),
            PostingRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .options(selectinload(PostingRun.project), selectinload(PostingRun.creator))
        .order_by(PostingRun.id.desc())
        .limit(10)
    )
    recent_q = (
        select(PostingRun)
        .where(
            PostingRun.deleted_at.is_(None),
            PostingRun.status.in_(FINISHED_RUN_STATUSES),
        )
        .options(selectinload(PostingRun.project), selectinload(PostingRun.creator))
        .order_by(PostingRun.id.desc())
        .limit(10)
    )
    if scope is not None:
        active_list_q = active_list_q.where(scope)
        recent_q = recent_q.where(scope)

    active_runs = list((await session.execute(active_list_q)).scalars().unique().all())
    recent_runs = list((await session.execute(recent_q)).scalars().unique().all())

    return {
        "scope": "all" if scope is None else "limited",
        "cards": {
            "active_runs": active_runs_count,
            "pending_texts": pending_texts_count,
            "posts_24h": posts_today,
            "failed_24h": failed_today,
            "wp_sites_active": sites_active,
            "wp_credentials_valid": cred_valid,
        },
        "active_runs": active_runs,
        "recent_runs": recent_runs,
    }
