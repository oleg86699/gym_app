"""
TaskIQ task для распаковки zip-архива с .txt текстами.

Шаги:
1. Читаем zip из MinIO (`uploads-tmp/{run_id}/source.zip`).
2. Для каждого .txt в архиве:
   - извлекаем `<title>`, остальное — body
   - считаем sha256(content)
   - заливаем в MinIO `text-items/{project_id}/{run_id}/{uuid}.txt`
   - собираем строку TextItem
3. Bulk insert батчами 500 шт (избегаем N+1).
4. ANALYZE text_items (после массовой вставки, чтобы планировщик не ходил по
   старой статистике).
5. Обновляем PostingRun: status → scheduled (если scheduled_for) или queued.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text, update

from core.config import settings
from core.db import WriteSession
from core.storage import storage
from core.taskiq_app import broker
from infrastructure.db.models import (
    CELERY_PRIORITY_MAP,
    PostingRun,
    PostingRunStatus,
    TextItem,
    TextItemStatus,
)

log = structlog.get_logger(__name__)

BATCH_SIZE = 500

# Простой regex для извлечения <title>...</title> (case-insensitive, multiline).
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def _parse_text_file(raw: bytes) -> tuple[str, str]:
    """
    Из .txt контента вытащить (title, body).
    - title: содержимое первого <title>...</title> (без тегов)
    - body: всё после </title> (или весь контент, если title нет)
    """
    from core.text_decode import decode_text
    text_content = decode_text(raw).strip()
    m = _TITLE_RE.search(text_content)
    if not m:
        return ("", text_content)
    title = m.group(1).strip()
    body = text_content[m.end():].strip()
    return (title, body)


async def _flush_text_items(rows: list[dict]) -> int:
    """B1: вставить тела в texts (returning id), проставить text_id и вставить
    text_items. Возвращает кол-во вставленных items. Тело носится в '__body__'."""
    from domain.texts import create_texts

    texts_rows = [{
        "body": r["__body__"],
        "title": r.get("title"),
        "lang": r.get("lang"),
        "source": "human",
        "content_hash": r["content_hash"],
    } for r in rows]
    async with WriteSession() as s:
        ids = await create_texts(s, texts_rows)
        for r, tid in zip(rows, ids, strict=True):
            r["text_id"] = tid
            r.pop("__body__", None)
        await s.execute(TextItem.__table__.insert(), rows)
        await s.commit()
    return len(rows)


@broker.task(task_name="postings.unpack_archive")
async def unpack_archive(run_id: int) -> dict:
    """Распаковать source.zip → создать text_items."""
    log_ctx = log.bind(run_id=run_id)
    log_ctx.info("unpack.start")

    async with WriteSession() as session:
        run = await session.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            log_ctx.error("unpack.run_not_found")
            return {"ok": False, "error": "run not found"}

        if not run.source_archive_storage_key:
            log_ctx.error("unpack.no_source_key")
            await _mark_failed(session, run_id, "no source archive")
            return {"ok": False, "error": "no source archive"}

        project_id = run.project_id
        storage_key = run.source_archive_storage_key
        # Фаза A: домены проекта — для разбора «наших» бэклинков из текстов.
        from infrastructure.db.models import ProjectDomain
        project_domains = list(
            (await session.scalars(
                select(ProjectDomain.domain).where(ProjectDomain.project_id == project_id)
            )).all()
        )

    # Скачиваем zip из MinIO (за пределами SQL-сессии — IO)
    try:
        zip_bytes = storage.get_bytes(settings.MINIO_BUCKET_UPLOADS, storage_key)
    except Exception as e:
        log_ctx.exception("unpack.download_failed", error=str(e))
        async with WriteSession() as s:
            await _mark_failed(s, run_id, f"download failed: {e}")
        return {"ok": False, "error": str(e)}

    # Распаковка в памяти + загрузка text_items в MinIO + сбор для bulk insert
    text_items_data: list[dict] = []
    total_inserted = 0
    skipped_invalid = 0
    skipped_too_big = 0
    skipped_non_txt = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Защита от zip-bomb: 1 МБ на файл, 10К файлов на архив
                if info.file_size > 1 * 1024 * 1024:
                    skipped_too_big += 1
                    continue
                # Фильтр macOS metadata (resource forks):
                #   __MACOSX/foo/._original.txt  — 212 байт бинарного мусора
                #   ._something.txt              — то же если zip-нули иначе
                #   .DS_Store                    — Finder metadata
                # Эти файлы пишет zip-команда на macOS «для совместимости».
                basename = info.filename.rsplit("/", 1)[-1]
                if (
                    "__MACOSX/" in info.filename
                    or basename.startswith("._")
                    or basename == ".DS_Store"
                ):
                    skipped_invalid += 1
                    continue
                if not info.filename.lower().endswith(".txt"):
                    skipped_non_txt += 1
                    continue

                try:
                    raw = zf.read(info)
                except Exception as e:
                    log_ctx.warning("unpack.read_failed", filename=info.filename, error=str(e))
                    skipped_invalid += 1
                    continue

                if not raw.strip():
                    skipped_invalid += 1
                    continue

                title, body = _parse_text_file(raw)
                # Санитизация: чиним битый HTML из источника (ссылки без кавычек,
                # хвостовые кавычки в href, кривые теги) ДО анализа и хеша — чтобы
                # link_url, content_hash и публикуемое тело были корректными.
                from domain.text_links import analyze_text, sanitize_text_html
                body = sanitize_text_html(body) or body
                # Фаза A: разбор ссылок/анкоров + язык + disambiguation целевого
                # бэклинка по доменам проекта. Неоднозначные → needs_review.
                analysis = analyze_text(body, project_domains)
                # Для дедупа — хешируем normalized content (body, title не учитываем)
                content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                obj_uuid = uuid.uuid4().hex
                storage_key_item = f"{project_id}/{run_id}/{obj_uuid}.txt"

                try:
                    storage.put_bytes(
                        settings.MINIO_BUCKET_TEXT_ITEMS,
                        storage_key_item,
                        raw,
                        content_type="text/html; charset=utf-8",
                    )
                except Exception as e:
                    log_ctx.warning("unpack.upload_failed", filename=info.filename, error=str(e))
                    skipped_invalid += 1
                    continue

                text_items_data.append({
                    "posting_run_id": run_id,
                    "project_id": project_id,
                    "storage_key": storage_key_item,
                    "original_filename": info.filename[:500],
                    "title": (title or None) and title[:1000],
                    "content_hash": content_hash,
                    "byte_size": len(raw),
                    # needs_review (нет/неоднозначен «наш» домен) → в постинг не идёт
                    "status": (TextItemStatus.NEEDS_REVIEW.value
                               if analysis.needs_review else TextItemStatus.PENDING.value),
                    "link_url": analysis.target_link,
                    "link_anchor": (analysis.target_anchor or None) and analysis.target_anchor[:500],
                    "target_domain": analysis.target_domain,
                    "lang": analysis.lang,
                    "link_candidates": analysis.candidates_as_dicts() or None,
                    # B1: тело уходит в texts, тут временно носим его до flush
                    "__body__": body,
                })

                # Сливаем батчами в БД чтобы не накапливать всё в памяти при больших архивах
                if len(text_items_data) >= BATCH_SIZE:
                    total_inserted += await _flush_text_items(text_items_data)
                    text_items_data = []

    except zipfile.BadZipFile as e:
        log_ctx.exception("unpack.bad_zip", error=str(e))
        async with WriteSession() as s:
            await _mark_failed(s, run_id, f"bad zip: {e}")
        return {"ok": False, "error": str(e)}

    # Финальный батч
    if text_items_data:
        total_inserted += await _flush_text_items(text_items_data)

    if total_inserted == 0:
        async with WriteSession() as s:
            await _mark_failed(s, run_id, "no valid .txt files found in archive")
        return {
            "ok": False,
            "error": "no valid texts",
            "skipped_invalid": skipped_invalid,
            "skipped_too_big": skipped_too_big,
            "skipped_non_txt": skipped_non_txt,
        }

    # ANALYZE — освежаем статистику планировщика после большой вставки
    async with WriteSession() as s:
        await s.execute(text("ANALYZE text_items"))

        # Финальный статус после unpack:
        #   - scheduled_for в будущем → SCHEDULED (cron dispatch_scheduled_runs
        #     автоматически переведёт в queued когда время придёт)
        #   - иначе → READY (ждёт ручного Start от юзера через UI/API).
        #
        # До 2026-06: автоматически переводили в QUEUED и сразу постили.
        # Поменяли на READY чтобы юзер мог проверить параметры (proxy pool,
        # posting_method, valid creds count) и явно нажать Start.
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run disappeared"}

        # Drip-feed: размазываем not_before по окну [старт, старт+spread_days].
        # Старт = scheduled_for (если в будущем), иначе now. rn=0 → старт (постится
        # сразу при запуске), rn=max → конец окна. Воркер постит due-порцию и
        # перевзводит run в scheduled до следующей.
        if run.spread_days and run.spread_days > 0 and total_inserted > 0:
            now_ts = datetime.now(UTC)
            window_start = (
                run.scheduled_for
                if (run.scheduled_for and run.scheduled_for > now_ts)
                else now_ts
            )
            await s.execute(
                text(
                    """
                    WITH o AS (
                        SELECT id,
                               (row_number() OVER (ORDER BY id) - 1)::float AS rn,
                               GREATEST(count(*) OVER () - 1, 1)::float AS denom
                        FROM text_items WHERE posting_run_id = :rid
                    )
                    UPDATE text_items t
                    SET not_before = (:ws)::timestamptz
                                     + make_interval(secs => (o.rn / o.denom) * :win)
                    FROM o WHERE t.id = o.id
                    """
                ),
                {"rid": run_id, "ws": window_start, "win": run.spread_days * 86400},
            )

        if run.scheduled_for and run.scheduled_for > datetime.now(UTC):
            new_status = PostingRunStatus.SCHEDULED.value
        else:
            new_status = PostingRunStatus.READY.value

        await s.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(status=new_status, total_texts=total_inserted)
        )
        await s.commit()
    # Celery НЕ дёргаем — это делает endpoint /postings/{id}/start.

    log_ctx.info(
        "unpack.done",
        inserted=total_inserted,
        skipped_invalid=skipped_invalid,
        skipped_too_big=skipped_too_big,
        skipped_non_txt=skipped_non_txt,
        new_status=new_status,
    )
    return {
        "ok": True,
        "inserted": total_inserted,
        "skipped_invalid": skipped_invalid,
        "skipped_too_big": skipped_too_big,
        "skipped_non_txt": skipped_non_txt,
        "status": new_status,
    }


async def _mark_failed(session, run_id: int, error: str) -> None:
    await session.execute(
        update(PostingRun)
        .where(PostingRun.id == run_id)
        .values(status=PostingRunStatus.FAILED.value)
    )
    await session.commit()
    log.bind(run_id=run_id).warning("unpack.marked_failed", error=error)
