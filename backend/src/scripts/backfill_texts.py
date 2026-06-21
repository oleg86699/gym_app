"""B1 backfill: перенести тела существующих text_items из MinIO в таблицу texts.

Идемпотентно — обрабатывает только items с text_id IS NULL и непустым
storage_key. Запуск:
    python -m scripts.backfill_texts
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select, update

from core.db import WriteSession
from domain.texts import create_texts
from domain.texts.service import _read_minio
from infrastructure.db.models import TextItem

log = structlog.get_logger(__name__)

BATCH = 200


async def backfill() -> dict:
    migrated = 0
    skipped_empty = 0
    while True:
        async with WriteSession() as s:
            items = list((await s.scalars(
                select(TextItem)
                .where(TextItem.text_id.is_(None), TextItem.storage_key.isnot(None))
                .limit(BATCH)
            )).all())
            if not items:
                break
            rows: list[dict] = []
            targets: list[int] = []
            for it in items:
                body = _read_minio(it.storage_key)
                if not body:
                    skipped_empty += 1
                    continue
                rows.append({
                    "body": body,
                    "title": it.title,
                    "lang": it.lang,
                    "source": "human",
                    "content_hash": it.content_hash,
                })
                targets.append(it.id)
            if not rows:
                # все в этом окне пустые — помечаем заглушкой чтобы не зациклиться
                # (ставим text_id у пустых на... нет; просто выходим если прогресса нет)
                break
            ids = await create_texts(s, rows)
            for item_id, tid in zip(targets, ids, strict=True):
                await s.execute(
                    update(TextItem).where(TextItem.id == item_id).values(text_id=tid)
                )
            await s.commit()
            migrated += len(rows)
            log.info("backfill_texts.batch", migrated=migrated, skipped_empty=skipped_empty)
            if len(items) < BATCH and not skipped_empty:
                break
    return {"migrated": migrated, "skipped_empty": skipped_empty}


if __name__ == "__main__":
    res = asyncio.run(backfill())
    print(f"backfill done: migrated={res['migrated']} skipped_empty={res['skipped_empty']}")
