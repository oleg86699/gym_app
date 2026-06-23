"""Единая Global Queue — снапшот ВСЕЙ активной работы в одном месте.

Объединяет «полосы» (lanes), которые крутятся в разных воркерах/процессах:
  - posting   — posting-run-ы (Celery): обычные посты + сквозные/homepage ссылки
  - validation — батч валидации кредов (TaskIQ): глобальное состояние в Redis
  - (generation — задел на будущее: LLM-генерация контента)

Плюс индикатор throttled: насколько занят глобальный постинг-лимитер
(in_use/limit). Это «hard limiter дефицитного ресурса» из плана балансировки —
показываем оператору, упёрлись ли мы в потолок и всё ли «двигается понемногу».
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.concurrency import posting_limiter
from infrastructure.db.models import (
    PostingRun,
    PostingRunStatus,
    WpBatchStatus,
    WpCredential,
    WpImportBatch,
)

# Статусы, которые считаем «в очереди / в работе» (не draft, не терминальные).
# Global Queue = только АКТИВНАЯ нагрузка на сервер прямо сейчас (running) +
# то, что вот-вот закрутится (queued/unpacking). Всё, что СТОИТ и ждёт действий
# (scheduled — спит между drip-порциями; paused — ручная пауза; need_more_admins
# / needs_review — ждут юзера), сюда НЕ попадает: иначе фоновые drip-раны и
# приостановленные забивают вывод, и нельзя оценить реальную загрузку.
_ACTIVE_STATUSES = (
    PostingRunStatus.UNPACKING.value,
    PostingRunStatus.QUEUED.value,
    PostingRunStatus.RUNNING.value,
)

# Порядок сортировки: «горячие» сверху.
_STATUS_RANK = {
    PostingRunStatus.RUNNING.value: 0,
    PostingRunStatus.QUEUED.value: 1,
    PostingRunStatus.UNPACKING.value: 2,
}


def _progress_pct(posted: int, failed: int, skipped: int, total: int) -> int:
    if total <= 0:
        return 0
    done = posted + failed + skipped
    return min(100, round(done * 100 / total))


async def _posting_lane(session: AsyncSession) -> list[dict]:
    rows = (
        await session.scalars(
            select(PostingRun)
            .where(PostingRun.status.in_(_ACTIVE_STATUSES),
                   PostingRun.deleted_at.is_(None))
        )
    ).all()
    # «Сгенерировано» (айтемы с текстом) per ран — для dual-бара ген/пост. Один
    # групповой запрос на все активные раны (их единицы).
    gen_counts: dict[int, int] = {}
    run_ids = [r.id for r in rows]
    if run_ids:
        from infrastructure.db.models import TextItem
        gc = await session.execute(
            select(TextItem.posting_run_id, func.count(TextItem.id))
            .where(TextItem.posting_run_id.in_(run_ids), TextItem.text_id.isnot(None))
            .group_by(TextItem.posting_run_id))
        gen_counts = {int(row[0]): int(row[1]) for row in gc.all()}
    items: list[dict] = []
    for r in rows:
        total = r.total_texts or 0
        gp = r.gen_params or {}
        items.append({
            "generated": gen_counts.get(r.id, 0),
            "id": r.id,
            "name": r.name,
            "project_id": r.project_id,
            "task_type": r.task_type,           # post | sitewide_link | homepage_link
            "status": r.status,
            "total": total,
            "posted": r.posted_count or 0,
            "failed": r.failed_count or 0,
            "skipped": r.skipped_count or 0,
            "progress_pct": _progress_pct(
                r.posted_count or 0, r.failed_count or 0, r.skipped_count or 0, total),
            # Генерация (csv_campaign): пока идёт AI-генерация ран в статусе
            # unpacking, но грузит сервер — отдаём прогресс генерации (красный бар).
            "gen_done": gp.get("gen_done"),
            "gen_total": gp.get("gen_total"),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "last_progress_at": r.last_progress_at.isoformat() if r.last_progress_at else None,
            "heartbeat_at": r.worker_heartbeat_at.isoformat() if r.worker_heartbeat_at else None,
            "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
        })
    items.sort(key=lambda x: (_STATUS_RANK.get(x["status"], 9), -(x["id"])))
    return items


async def _validation_lane(session: AsyncSession) -> dict | None:
    """Валидация кредов. Две полосы:
      • batch-валидация (status=validating в wp_import_batches) — приоритет,
        если идёт прямо сейчас (это и есть «запустил батч»);
      • пул-валидатор (global state в Redis) — on-demand перепроверка пула.
    None если ничего не идёт и нет завершённого состояния.
    """
    # 1) идущие batch-валидации (из БД — надёжно, не зависит от Redis-стейта)
    brows = (await session.execute(
        select(WpImportBatch.id, WpImportBatch.name, WpImportBatch.validation_started_at)
        .where(WpImportBatch.status == WpBatchStatus.VALIDATING.value,
               WpImportBatch.deleted_at.is_(None))
    )).all()
    batches: list[dict] = []
    if brows:
        bids = [r[0] for r in brows]
        counts = {r[0]: r for r in (await session.execute(
            select(
                WpCredential.import_batch_id,
                func.count(WpCredential.id),
                func.count(WpCredential.id).filter(WpCredential.cred_status == "valid"),
                func.count(WpCredential.id).filter(WpCredential.cred_status == "invalid"),
                func.count(WpCredential.id).filter(WpCredential.cred_status == "transient"),
                func.count(WpCredential.id).filter(WpCredential.cred_status == "pending"),
            ).where(WpCredential.import_batch_id.in_(bids),
                    WpCredential.deleted_at.is_(None))
            .group_by(WpCredential.import_batch_id)
        )).all()}
        for bid, name, started in brows:
            c = counts.get(bid)
            total = int(c[1]) if c else 0
            valid = int(c[2]) if c else 0
            invalid = int(c[3]) if c else 0
            transient = int(c[4]) if c else 0
            pending = int(c[5]) if c else 0
            done = total - pending
            batches.append({
                "batch_id": bid, "name": name, "running": True,
                "total": total, "done": done, "valid": valid,
                "invalid": invalid, "transient_errors": transient,
                "progress_pct": min(100, round(done * 100 / total)) if total else 0,
                "started_at": started.isoformat() if started else None,
            })
    if batches:
        agg_total = sum(b["total"] for b in batches)
        agg_done = sum(b["done"] for b in batches)
        return {
            "running": True,
            "kind": "batch",
            "scope": batches[0]["name"] if len(batches) == 1 else f"{len(batches)} батча",
            "total": agg_total,
            "done": agg_done,
            "valid": sum(b["valid"] for b in batches),
            "invalid": sum(b["invalid"] for b in batches),
            "transient_errors": sum(b["transient_errors"] for b in batches),
            "progress_pct": min(100, round(agg_done * 100 / agg_total)) if agg_total else 0,
            "started_at": batches[0]["started_at"],
            "finished_at": None,
            "batches": batches,
        }

    # 2) пул-валидатор (Redis global state)
    try:
        from domain.wp_validation.service import get_state
        st = await get_state()
    except Exception:
        return None
    if not st.running and st.total == 0 and not st.finished_at:
        return None
    total = st.total or 0
    return {
        "running": st.running,
        "kind": "pool",
        "scope": st.scope,
        "total": total,
        "done": st.done,
        "valid": st.valid,
        "invalid": st.invalid,
        "transient_errors": st.transient_errors,
        "progress_pct": min(100, round((st.done or 0) * 100 / total)) if total else 0,
        "started_at": st.started_at,
        "finished_at": st.finished_at,
    }


# Статусы активной перепроверки бэклинков (link-check).
_LINK_CHECK_STATUSES = ("queued", "running")


async def _link_check_lane(session: AsyncSession) -> list[dict]:
    """Активные перепроверки проставленных ссылок (link-check) завершённых
    прогонов — отдельный (фиолетовый) тип нагрузки: внешние GET-ы страниц постов,
    запущенные вручную после постинга. Видно, чем занят сервер."""
    rows = (await session.scalars(
        select(PostingRun)
        .where(PostingRun.link_check_status.in_(_LINK_CHECK_STATUSES),
               PostingRun.deleted_at.is_(None))
    )).all()
    items: list[dict] = []
    for r in rows:
        total = r.link_check_total or 0
        done = r.link_check_done or 0
        items.append({
            "id": r.id,
            "name": r.name,
            "project_id": r.project_id,
            "status": r.link_check_status,
            "total": total,
            "done": done,
            "valid": r.link_check_valid or 0,
            "progress_pct": min(100, round(done * 100 / total)) if total else 0,
            "started_at": r.link_check_at.isoformat() if r.link_check_at else None,
        })
    items.sort(key=lambda x: (0 if x["status"] == "running" else 1, -x["id"]))
    return items


async def get_queue_snapshot(session: AsyncSession) -> dict:
    posting = await _posting_lane(session)
    validation = await _validation_lane(session)
    link_checks = await _link_check_lane(session)

    limit = 80
    try:
        from domain.app_settings.service import get_app_settings
        limit = int((await get_app_settings(session)).global_posting_concurrency)
    except Exception:
        pass
    in_use = await posting_limiter.in_use()

    running_posting = sum(1 for p in posting if p["status"] == PostingRunStatus.RUNNING.value)
    return {
        "limiter": {
            "name": "posting",
            "in_use": in_use,
            "limit": limit,
            "throttled": in_use >= limit,
            "utilization_pct": min(100, round(in_use * 100 / limit)) if limit else 0,
        },
        "posting": posting,
        "validation": validation,
        "link_checks": link_checks,
        "summary": {
            "posting_active": len(posting),
            "posting_running": running_posting,
            "validation_running": bool(validation and validation.get("running")),
            "link_check_active": len(link_checks),
        },
    }
