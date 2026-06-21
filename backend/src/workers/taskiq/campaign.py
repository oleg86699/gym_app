"""TaskIQ: кампания (links, anchor, counts) → reuse из библиотеки texts.

На каждую строку counts задач anchor→link; под каждую берём текст из
библиотеки, вычищаем старую ссылку и инжектим новую (reuse-движок).
Генерация новых текстов — отдельно (генератор позже).
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, text as sql, update

from core.config import settings
from core.db import WriteSession
from core.storage import storage
from core.taskiq_app import broker
from infrastructure.db.models import PostingRun, PostingRunStatus

log = structlog.get_logger(__name__)


@broker.task(task_name="postings.process_campaign")
async def process_campaign(run_id: int) -> dict:
    log_ctx = log.bind(run_id=run_id, kind="campaign")
    log_ctx.info("campaign.start")

    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        if not run.source_archive_storage_key:
            await _fail(s, run_id, "no source file")
            return {"ok": False, "error": "no source file"}
        project_id = run.project_id
        storage_key = run.source_archive_storage_key
        spread_days = run.spread_days or 0
        scheduled_for = run.scheduled_for

    try:
        content = storage.get_bytes(settings.MINIO_BUCKET_UPLOADS, storage_key)
    except Exception as e:
        async with WriteSession() as s:
            await _fail(s, run_id, f"download failed: {e}")
        return {"ok": False, "error": str(e)}

    from domain.csv_inputs import parse_campaign
    try:
        tasks = parse_campaign(content, storage_key)
    except ValueError as e:
        async with WriteSession() as s:
            await _fail(s, run_id, f"parse error: {e}")
        return {"ok": False, "error": str(e)}
    if not tasks:
        async with WriteSession() as s:
            await _fail(s, run_id, "no valid rows (need column links)")
        return {"ok": False, "error": "no rows"}

    from domain.reuse import generate_reuse_items
    async with WriteSession() as s:
        total = await generate_reuse_items(s, run_id=run_id, project_id=project_id, tasks=tasks)

    if total == 0:
        async with WriteSession() as s:
            await _fail(s, run_id,
                        "no library texts to reuse (залей тексты или выбери reuse, когда библиотека не пуста)")
        return {"ok": False, "error": "no library texts"}

    # spread + финальный статус
    async with WriteSession() as s:
        if spread_days and spread_days > 0:
            from datetime import UTC, datetime
            now_ts = datetime.now(UTC)
            ws = scheduled_for if (scheduled_for and scheduled_for > now_ts) else now_ts
            await s.execute(sql("""
                WITH o AS (SELECT id,(row_number() OVER (ORDER BY id)-1)::float AS rn,
                                  GREATEST(count(*) OVER ()-1,1)::float AS denom
                           FROM text_items WHERE posting_run_id=:rid)
                UPDATE text_items t SET not_before=(:ws)::timestamptz
                    + make_interval(secs => (o.rn/o.denom)*:win)
                FROM o WHERE t.id=o.id
            """), {"rid": run_id, "ws": ws, "win": spread_days * 86400})
        from datetime import UTC, datetime
        new_status = (PostingRunStatus.SCHEDULED.value
                      if (scheduled_for and scheduled_for > datetime.now(UTC))
                      else PostingRunStatus.READY.value)
        await s.execute(update(PostingRun).where(PostingRun.id == run_id)
                        .values(status=new_status, total_texts=total))
        await s.commit()

    log_ctx.info("campaign.done", inserted=total, status=new_status)
    return {"ok": True, "inserted": total, "status": new_status}


async def _fail(session, run_id: int, msg: str) -> None:
    await session.execute(update(PostingRun).where(PostingRun.id == run_id)
                          .values(status=PostingRunStatus.FAILED.value))
    await session.commit()
    log.warning("campaign.failed", run_id=run_id, error=msg)
