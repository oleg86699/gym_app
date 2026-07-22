"""
Posting runs service: создание прогона, листинг, контроль статусов.

Сам постинг (status=running) подъедет в блоке 4 (Celery worker). Сейчас
прогон создаётся → загружаемый zip распаковывается TaskIQ-таской в text_items
→ статус scheduled/queued.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import BigInteger, case, func, literal, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from infrastructure.db.models import (
    AdminUser,
    PostingRun,
    PostingRunStatus,
    Project,
    TextItem,
    TextItemStatus,
)


# ─── Пул доступов: резолв списка доменов ──────────────────────────────

# Кэш списков доменов по MinIO-ключу (ключ → immutable список). Заполняется
# лениво в воркере, чтобы не дёргать MinIO на каждой итерации подбора сайтов.
_RUN_DOMAINS_CACHE: dict[str, list[str]] = {}


def resolve_site_domains(gen_params) -> list[str] | None:
    """Список доменов пула для прогона из gen_params:
    - inline `site_domains` (маленький список, лежит прямо в gen_params), ИЛИ
    - `site_domains_key` — большой список, загруженный файлом в MinIO; читаем
      один раз и кэшируем по ключу. None → ограничения по домену нет."""
    gp = gen_params or {}
    inline = gp.get("site_domains")
    if inline:
        return inline
    key = gp.get("site_domains_key")
    if not key:
        return None
    if key not in _RUN_DOMAINS_CACHE:
        from core.config import settings
        from core.storage import storage
        try:
            raw = storage.get_bytes(settings.MINIO_BUCKET_UPLOADS, key)
            _RUN_DOMAINS_CACHE[key] = [d.strip() for d in raw.decode("utf-8").splitlines() if d.strip()]
        except Exception:
            _RUN_DOMAINS_CACHE[key] = []
    return _RUN_DOMAINS_CACHE[key] or None


# ─── Access / scope ───────────────────────────────────────────────────


def can_view_run(viewer: AdminUser, run: PostingRun, project: Project) -> bool:
    """Прогон видит тот, кто видит проект."""
    from domain.projects.service import can_view_project

    return can_view_project(viewer, project)


def can_manage_run(viewer: AdminUser, run: PostingRun, project: Project) -> bool:
    """Управлять прогоном может owner проекта / group_admin его группы / super_admin."""
    from domain.projects.service import can_manage_project

    return can_manage_project(viewer, project)


# ─── Backpressure ─────────────────────────────────────────────────────


async def count_active_runs_for_user(session: AsyncSession, user_id: int) -> int:
    """Сколько прогонов сейчас в активных статусах у юзера."""
    active_statuses = {
        PostingRunStatus.UNPACKING.value,
        PostingRunStatus.SCHEDULED.value,
        PostingRunStatus.QUEUED.value,
        PostingRunStatus.RUNNING.value,
        PostingRunStatus.PAUSED.value,
    }
    stmt = select(func.count(PostingRun.id)).where(
        PostingRun.created_by == user_id,
        PostingRun.deleted_at.is_(None),
        PostingRun.status.in_(active_statuses),
    )
    return (await session.execute(stmt)).scalar_one()


# ─── Queries ──────────────────────────────────────────────────────────


def _run_load_opts():
    return (
        selectinload(PostingRun.project),
        selectinload(PostingRun.creator),
        selectinload(PostingRun.deleted_by_user),
    )


async def list_runs_for_viewer(
    session: AsyncSession,
    *,
    viewer: AdminUser,
    after_id: int | None = None,
    limit: int = 50,
    statuses: list[str] | None = None,
    project_id: int | None = None,
    created_by: int | None = None,
    search: str | None = None,
    include_deleted: bool = False,
) -> list[PostingRun]:
    """
    Все runs которые viewer может видеть.

    Scope наследуется от Project (через _visible_projects_filter):
    - super_admin → все
    - group_admin → runs в проектах своей группы
    - user → runs в своих/расшаренных проектах
    """
    from domain.projects.service import _visible_projects_filter

    stmt = (
        select(PostingRun)
        .options(*_run_load_opts())
        .order_by(PostingRun.id.desc())
        .limit(limit + 1)
    )
    if not (include_deleted and viewer.is_super_admin):
        stmt = stmt.where(PostingRun.deleted_at.is_(None))

    project_filter = _visible_projects_filter(viewer)
    if project_filter is not None:
        stmt = stmt.join(Project, Project.id == PostingRun.project_id).where(project_filter)

    if project_id is not None:
        stmt = stmt.where(PostingRun.project_id == project_id)
    if created_by is not None:
        stmt = stmt.where(PostingRun.created_by == created_by)
    if statuses:
        stmt = stmt.where(PostingRun.status.in_(statuses))
    if search:
        stmt = stmt.where(PostingRun.name.ilike(f"%{search.strip()}%"))
    if after_id:
        stmt = stmt.where(PostingRun.id < after_id)

    return list((await session.execute(stmt)).scalars().unique().all())


async def list_runs_for_project(
    session: AsyncSession,
    *,
    project_id: int,
    after_id: int | None = None,
    limit: int = 100,
    include_deleted: bool = False,
) -> list[PostingRun]:
    stmt = (
        select(PostingRun)
        .where(PostingRun.project_id == project_id)
        .options(*_run_load_opts())
        .order_by(PostingRun.id.desc())
        .limit(limit + 1)
    )
    if not include_deleted:
        stmt = stmt.where(PostingRun.deleted_at.is_(None))
    if after_id:
        stmt = stmt.where(PostingRun.id < after_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_run(
    session: AsyncSession, run_id: int, *, include_deleted: bool = False,
) -> PostingRun | None:
    stmt = select(PostingRun).where(PostingRun.id == run_id).options(*_run_load_opts())
    if not include_deleted:
        stmt = stmt.where(PostingRun.deleted_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


# ─── Create ───────────────────────────────────────────────────────────


async def create_run(
    session: AsyncSession,
    *,
    project: Project,
    creator: AdminUser,
    name: str,
    publish_from: date | None,
    publish_to: date | None,
    concurrency: int,
    timeout_seconds: int,
    priority: str,
    scheduled_for: datetime | None,
    source_archive_storage_key: str,
    proxy_id: int | None = None,
    proxy_selector: str | None = None,
    posting_method: str = "auto",
    spread_days: int = 0,
    max_posts_per_site: int = 1,
    post_verify: str = "mark",
    pool_fallback: bool = False,
) -> PostingRun:
    run = PostingRun(
        project_id=project.id,
        created_by=creator.id,
        name=name.strip(),
        status=PostingRunStatus.UNPACKING.value,
        publish_from=publish_from,
        publish_to=publish_to,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        priority=priority,
        scheduled_for=scheduled_for,
        spread_days=spread_days,
        source_archive_storage_key=source_archive_storage_key,
        proxy_id=proxy_id,
        proxy_selector=proxy_selector,
        posting_method=posting_method,
        max_posts_per_site=max_posts_per_site,
        post_verify=post_verify,
        pool_fallback=pool_fallback,
    )
    session.add(run)
    await session.commit()
    refreshed = await get_run(session, run.id)
    assert refreshed is not None
    return refreshed


async def set_run_status(
    session: AsyncSession,
    *,
    run_id: int,
    status: PostingRunStatus,
    total_texts: int | None = None,
) -> None:
    """Атомарный апдейт статуса (используется TaskIQ после распаковки)."""
    values: dict = {"status": status.value}
    if total_texts is not None:
        values["total_texts"] = total_texts

    await session.execute(update(PostingRun).where(PostingRun.id == run_id).values(**values))
    await session.commit()


# ─── Run progress (на основе денорм-счётчиков) ───────────────────────


async def run_progress_counts(session: AsyncSession, run_id: int) -> dict[str, int]:
    """
    Полный разрез по статусам text_items. Денорм-счётчики на run-е
    учитывают только posted/failed/skipped; pending/posting считаем
    отдельным запросом для точного отображения в UI.
    """
    stmt = select(TextItem.status, func.count(TextItem.id)).where(
        TextItem.posting_run_id == run_id
    ).group_by(TextItem.status)
    by_status = dict((row[0], int(row[1])) for row in (await session.execute(stmt)).all())
    # «Сгенерировано» = айтемы с готовым текстом (text_id NOT NULL) — для dual-бара
    # (красный=генерация, зелёный=постинг). Для не-gen ранов = total (текст уже есть).
    generated = await session.scalar(select(func.count(TextItem.id)).where(
        TextItem.posting_run_id == run_id, TextItem.text_id.isnot(None)))
    return {
        "total": sum(by_status.values()),
        "pending": by_status.get(TextItemStatus.PENDING.value, 0),
        "generating": by_status.get(TextItemStatus.GENERATING.value, 0),
        "posting": by_status.get(TextItemStatus.POSTING.value, 0),
        "posted": by_status.get(TextItemStatus.POSTED.value, 0),
        "failed": by_status.get(TextItemStatus.FAILED.value, 0),
        "skipped": by_status.get(TextItemStatus.SKIPPED.value, 0),
        "needs_review": by_status.get(TextItemStatus.NEEDS_REVIEW.value, 0),
        "generated": int(generated or 0),
    }


# ─── TextItems listing для run detail page ──────────────────────────


# Сортировка text_items: айтемы С текстом — выше пустых (gen_per_row: оригиналы
# вверху, чтобы не искать их среди пустых спин-плейсхолдеров). Для обычных ранов
# (у всех есть text_id) ключ = id → порядок не меняется. Один int → курсор-пагинация.
_ITEM_SORT_BUMP = 1_000_000_000_000
# айтемы с текстом (text_id NOT NULL) → 0+id (вверху); пустые → bump+id (внизу).
# case вместо cast(bool→bigint) — PG не кастит boolean в bigint напрямую.
_ITEM_SORT_KEY = (
    case((TextItem.text_id.is_(None), literal(_ITEM_SORT_BUMP, BigInteger)), else_=literal(0))
    + TextItem.id
)


def item_sort_key(it: TextItem) -> int:
    """Питон-версия _ITEM_SORT_KEY — для вычисления next_cursor в эндпоинте."""
    return (_ITEM_SORT_BUMP if it.text_id is None else 0) + it.id


async def list_text_items_for_run(
    session: AsyncSession,
    *,
    run_id: int,
    status: str | None = None,
    after_id: int | None = None,
    limit: int = 50,
    day: "date | None" = None,
) -> list[TextItem]:
    """Список text_items с подгруженными site/credential для UI таблицы.
    Айтемы с текстом — вверху (см. _ITEM_SORT_KEY). `after_id` тут = sort_key-курсор.
    `day` — фильтр по дню drip-размазки (date(not_before) == day)."""
    stmt = (
        select(TextItem)
        .where(TextItem.posting_run_id == run_id)
        .options(
            selectinload(TextItem.credential),
            selectinload(TextItem.site),
        )
        .order_by(_ITEM_SORT_KEY)
        .limit(limit + 1)
    )
    if day is not None:
        stmt = stmt.where(func.date(TextItem.not_before) == day)
    if status:
        # поддержка нескольких статусов через запятую («in-progress» = pending,posting)
        statuses = [s for s in status.split(",") if s]
        stmt = stmt.where(TextItem.status == statuses[0] if len(statuses) == 1
                          else TextItem.status.in_(statuses))
    if after_id:
        stmt = stmt.where(_ITEM_SORT_KEY > after_id)
    return list((await session.execute(stmt)).scalars().all())


async def run_day_stats(session: AsyncSession, run_id: int) -> list[dict]:
    """Агрегат айтемов рана по дням drip-размазки (date(not_before)):
    [{day, total, posted, pending, failed, generated}], по возрастанию дня. Для
    графика по дням в UI (только у drip-ранов — где not_before проставлен)."""
    day_col = func.date(TextItem.not_before)
    rows = (await session.execute(
        select(
            day_col.label("day"),
            func.count().label("total"),
            func.count().filter(
                TextItem.status == TextItemStatus.POSTED.value).label("posted"),
            func.count().filter(
                TextItem.status == TextItemStatus.FAILED.value).label("failed"),
            func.count().filter(TextItem.status.in_((
                TextItemStatus.PENDING.value, TextItemStatus.POSTING.value,
            ))).label("pending"),
            func.count().filter(
                TextItem.text_id.isnot(None)).label("generated"),
        ).where(
            TextItem.posting_run_id == run_id,
            TextItem.not_before.isnot(None),
        ).group_by(day_col).order_by(day_col)
    )).all()
    return [
        {"day": r.day.isoformat(), "total": int(r.total), "posted": int(r.posted),
         "failed": int(r.failed), "pending": int(r.pending),
         "generated": int(r.generated)}
        for r in rows
    ]


async def get_text_item(session: AsyncSession, item_id: int) -> TextItem | None:
    stmt = (
        select(TextItem)
        .where(TextItem.id == item_id)
        .options(
            selectinload(TextItem.credential),
            selectinload(TextItem.site),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# Айтем в активной работе воркера — удалять нельзя (гонка с постингом/генерацией).
_UNDELETABLE_ITEM_STATUSES: frozenset[str] = frozenset({
    TextItemStatus.POSTING.value,
    TextItemStatus.GENERATING.value,
})


async def delete_text_item(
    session: AsyncSession, *, run_id: int, item_id: int, actor_id: int | None = None,
) -> dict:
    """Hard-delete одного text_item прогона + пересчёт денорм-счётчиков рана.

    Если ран стоял в needs_review и после удаления не осталось needs_review/активных
    айтемов — финализируем ран в done (posted/failed/skipped — терминальные).
    Айтемы в активной работе (posting/generating) удалять запрещаем.
    """
    item = await session.scalar(
        select(TextItem).where(
            TextItem.id == item_id, TextItem.posting_run_id == run_id)
    )
    if item is None:
        return {"ok": False, "status": "not_found"}
    st = item.status
    if st in _UNDELETABLE_ITEM_STATUSES:
        return {"ok": False, "status": "active", "item_status": st}

    run = await session.scalar(select(PostingRun).where(PostingRun.id == run_id))

    await session.execute(sa_delete(TextItem).where(TextItem.id == item_id))
    # Декремент денорм-счётчиков рана (ADR-003). total всегда; posted/failed/skipped
    # — только если удаляемый айтем был в этом терминальном статусе.
    vals: dict = {"total_texts": PostingRun.total_texts - 1}
    if st == TextItemStatus.POSTED.value:
        vals["posted_count"] = PostingRun.posted_count - 1
    elif st == TextItemStatus.FAILED.value:
        vals["failed_count"] = PostingRun.failed_count - 1
    elif st == TextItemStatus.SKIPPED.value:
        vals["skipped_count"] = PostingRun.skipped_count - 1
    await session.execute(
        update(PostingRun).where(PostingRun.id == run_id).values(**vals))
    await session.commit()

    # Финализация: ран висел в needs_review из-за таких айтемов — если их (и любых
    # активных) больше нет, ран завершён.
    new_run_status: str | None = None
    if run is not None and run.status == PostingRunStatus.NEEDS_REVIEW.value:
        counts = await run_progress_counts(session, run_id)
        remaining = (counts["pending"] + counts["posting"] + counts["generating"]
                     + counts["needs_review"])
        if remaining == 0:
            await set_run_status(
                session, run_id=run_id, status=PostingRunStatus.DONE)
            new_run_status = PostingRunStatus.DONE.value

    return {"ok": True, "deleted_status": st, "run_status": new_run_status}


# Статусы, в которых разрешено редактировать содержимое.
# Запрещаем только POSTING (воркер прямо сейчас использует этот контент).
# Для POSTED правки сохраняются локально; для синка с WP нужна отдельная фича
# (wp.editPost), которой пока нет — UI это явно предупреждает.
EDITABLE_TEXT_STATUSES: frozenset[str] = frozenset(
    s.value for s in TextItemStatus if s != TextItemStatus.POSTING
)


async def update_text_item_meta(
    session: AsyncSession,
    *,
    item_id: int,
    title: str | None,
    byte_size: int,
    content_hash: str,
) -> None:
    """Обновить метаданные text_item после загрузки нового контента в MinIO."""
    await session.execute(
        update(TextItem)
        .where(TextItem.id == item_id)
        .values(title=(title or None) and title[:1000], byte_size=byte_size, content_hash=content_hash)
    )
    await session.commit()


# ─── Управление run-ом (pause / resume / cancel / retry) ─────────────


async def request_pause(session: AsyncSession, run_id: int) -> None:
    await session.execute(
        update(PostingRun).where(PostingRun.id == run_id).values(pause_requested=True)
    )
    await session.commit()


async def request_cancel(session: AsyncSession, run_id: int) -> None:
    await session.execute(
        update(PostingRun).where(PostingRun.id == run_id).values(cancel_requested=True)
    )
    await session.commit()


async def soft_delete_run(
    session: AsyncSession, run_id: int, *, actor_id: int | None = None,
) -> None:
    """Архивируем run — `deleted_at = now()` (+ кто удалил). Списки фильтруют по
    NULL; super_admin может смотреть с include_deleted. Полное удаление —
    purge_run (super only)."""
    await session.execute(
        update(PostingRun)
        .where(PostingRun.id == run_id)
        .values(deleted_at=datetime.now(UTC), deleted_by=actor_id)
    )
    await session.commit()


async def restore_run(session: AsyncSession, run_id: int) -> None:
    """super_admin: вернуть soft-deleted run из архива."""
    await session.execute(
        update(PostingRun)
        .where(PostingRun.id == run_id)
        .values(deleted_at=None, deleted_by=None)
    )
    await session.commit()


async def purge_run(session: AsyncSession, run_id: int) -> None:
    """super_admin: полное (hard) удаление run из БД. БД-каскад сносит его
    text_items и project_wp_used."""
    await session.execute(sa_delete(PostingRun).where(PostingRun.id == run_id))
    await session.commit()


async def request_resume(session: AsyncSession, run_id: int) -> bool:
    """
    Снимаем pause_requested. Если run в PAUSED или INTERRUPTED — возвращаем
    в QUEUED, чтобы caller перепослал в Celery. True = нужно re-enqueue.

    INTERRUPTED уже имеет text_items сброшенные posting→pending recovery-job-ом,
    так что воркер просто продолжит с того места, где остановился (state в БД,
    не в памяти — ADR-001).
    """
    run = await session.scalar(select(PostingRun).where(PostingRun.id == run_id))
    if run is None:
        return False
    resumable = {PostingRunStatus.PAUSED.value, PostingRunStatus.INTERRUPTED.value}
    needs_enqueue = run.status in resumable
    values: dict = {"pause_requested": False}
    if needs_enqueue:
        values["status"] = PostingRunStatus.QUEUED.value
        # Если резюмим из interrupted — сбрасываем finished_at чтобы run снова
        # считался активным, и подчищаем cancel_requested на всякий случай.
        if run.status == PostingRunStatus.INTERRUPTED.value:
            values["finished_at"] = None
            values["cancel_requested"] = False
    await session.execute(update(PostingRun).where(PostingRun.id == run_id).values(**values))
    await session.commit()
    return needs_enqueue


async def retry_failed_items(session: AsyncSession, run_id: int) -> tuple[int, bool]:
    """
    Перевести все failed text_items этого run-а обратно в pending.
    Если сам run в финальном статусе (done/failed/cancelled/interrupted/
    need_more_admins) — поднимаем его в queued.
    Возвращаем (сколько перезапустили, нужно_ли_re-enqueue).
    """
    result = await session.execute(
        update(TextItem)
        .where(
            TextItem.posting_run_id == run_id,
            TextItem.status == TextItemStatus.FAILED.value,
        )
        .values(status=TextItemStatus.PENDING.value, last_error=None)
    )
    retried = int(result.rowcount or 0)
    if retried == 0:
        await session.commit()
        return (0, False)

    run = await session.scalar(select(PostingRun).where(PostingRun.id == run_id))
    if run is None:
        await session.commit()
        return (retried, False)

    finalized = {
        PostingRunStatus.DONE.value,
        PostingRunStatus.FAILED.value,
        PostingRunStatus.CANCELLED.value,
        PostingRunStatus.INTERRUPTED.value,
        PostingRunStatus.NEED_MORE_ADMINS.value,
    }
    needs_enqueue = run.status in finalized
    if needs_enqueue:
        # Сбрасываем failed_count чтобы счётчик в UI стал корректным после retry.
        # posted_count оставляем (это валидные публикации).
        await session.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(
                status=PostingRunStatus.QUEUED.value,
                failed_count=PostingRun.failed_count - retried,
                cancel_requested=False,
                pause_requested=False,
                finished_at=None,
            )
        )
    else:
        await session.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(failed_count=PostingRun.failed_count - retried)
        )
    await session.commit()
    return (retried, needs_enqueue)
