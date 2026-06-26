"""TaskIQ: обработка csv-direct входа (link, anchor, text).

Данные заданы напрямую → дизамбигуация не нужна: тело → texts, link/anchor →
text_item (status=pending). Источник (csv/xlsx) лежит в MinIO uploads.
"""

from __future__ import annotations

import hashlib

import structlog
from sqlalchemy import select, text as sql, update

from core.config import settings
from core.db import WriteSession
from core.lang_detect import detect_language_from_html
from core.storage import storage
from domain.text_links import inject_link, normalize_domain, sanitize_text_html
from infrastructure.db.models import PostingRun, PostingRunStatus
from core.taskiq_app import broker

log = structlog.get_logger(__name__)


@broker.task(task_name="postings.process_csv_direct")
async def process_csv_direct(run_id: int) -> dict:
    """Распарсить csv/xlsx (link,anchor,text) → texts + text_items."""
    log_ctx = log.bind(run_id=run_id, kind="csv_direct")
    log_ctx.info("csv_direct.start")

    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        if not run.source_archive_storage_key:
            await _mark_failed(s, run_id, "no source file")
            return {"ok": False, "error": "no source file"}
        project_id = run.project_id
        storage_key = run.source_archive_storage_key
        spread_days = run.spread_days or 0
        scheduled_for = run.scheduled_for
        # Опц.: инжектить ссылку из строки в тело (по умолчанию НЕТ — тело как есть).
        inject = bool((run.gen_params or {}).get("csv_inject_link"))

    # читаем файл из MinIO
    try:
        content = storage.get_bytes(settings.MINIO_BUCKET_UPLOADS, storage_key)
    except Exception as e:
        async with WriteSession() as s:
            await _mark_failed(s, run_id, f"download failed: {e}")
        return {"ok": False, "error": str(e)}

    # парсим
    from domain.csv_inputs import parse_link_anchor_text
    try:
        rows = parse_link_anchor_text(content, storage_key)
    except ValueError as e:
        async with WriteSession() as s:
            await _mark_failed(s, run_id, f"parse error: {e}")
        return {"ok": False, "error": str(e)}

    if not rows:
        async with WriteSession() as s:
            await _mark_failed(s, run_id, "no valid rows (need columns link, anchor, text)")
        return {"ok": False, "error": "no valid rows"}

    from workers.taskiq.unpack import _flush_text_items, BATCH_SIZE

    total = 0
    batch: list[dict] = []
    for i, r in enumerate(rows):
        # По флагу — инжектим ссылку из строки в тело (то же правило, что в Reuse:
        # обернуть анкор/значимое слово, иначе вставить в абзац). Иначе тело как есть.
        body = (inject_link(r["text"], r["link"], r.get("anchor") or r["link"])
                if inject else r["text"])
        # Санитизация: чиним битый HTML из CSV-текста (некавыченные/незакрытые теги,
        # хвостовые кавычки в href) ДО хеша/сохранения.
        body = sanitize_text_html(body) or body
        batch.append({
            "posting_run_id": run_id,
            "project_id": project_id,
            "storage_key": None,           # тело только в texts
            "original_filename": f"csv-direct/{i + 1}",
            "title": None,
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "byte_size": len(body.encode("utf-8")),
            "status": "pending",           # ссылка задана явно → дизамбигуация не нужна
            "link_url": r["link"],
            "link_anchor": (r.get("anchor") or None) and r["anchor"][:500],
            "target_domain": normalize_domain(r["link"]),
            "lang": detect_language_from_html(body),
            "link_candidates": None,
            "__body__": body,
        })
        if len(batch) >= BATCH_SIZE:
            total += await _flush_text_items(batch)
            batch = []
    if batch:
        total += await _flush_text_items(batch)

    # spread (drip) + финальный статус — как в unpack
    async with WriteSession() as s:
        if spread_days and spread_days > 0 and total > 0:
            from datetime import UTC, datetime
            now_ts = datetime.now(UTC)
            ws = scheduled_for if (scheduled_for and scheduled_for > now_ts) else now_ts
            await s.execute(sql("""
                WITH o AS (SELECT id, (row_number() OVER (ORDER BY id)-1)::float AS rn,
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
        # Авто-привязка доменов явных ссылок к проекту («забыл добавить» safety-net).
        from domain.project_domains import autobind_link_domains
        await autobind_link_domains(s, project_id, [r.get("link") for r in rows])

    log_ctx.info("csv_direct.done", inserted=total, status=new_status)
    return {"ok": True, "inserted": total, "status": new_status}


async def _mark_failed(session, run_id: int, msg: str) -> None:
    await session.execute(update(PostingRun).where(PostingRun.id == run_id)
                          .values(status=PostingRunStatus.FAILED.value))
    await session.commit()
    log.warning("csv_direct.failed", run_id=run_id, error=msg)
