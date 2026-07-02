"""Единая библиотека текстов (B1): тела в БД (texts), MinIO как fallback.

Источник истины тела текста — texts.body (via text_items.text_id). Для старых
items, ещё не перенесённых из MinIO, read_item_body фоллбэчит на storage_key.
"""

from __future__ import annotations

import structlog
from sqlalchemy import insert, select, text as sql, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.storage import StorageError, storage
from infrastructure.db.models import Text

log = structlog.get_logger(__name__)


# Чистый текст-сниппет: вырезаем HTML-теги, берём первые N символов.
_SNIPPET = "left(regexp_replace(coalesce(body,''), '<[^>]+>', ' ', 'g'), 240)"


# Базовые метаданные текста для библиотеки. `:body_col` подставляем (snippet или
# полное body — для экспорта). reusable_only фильтрует reuse-кандидаты.
def _base_select(body_col: str) -> str:
    return f"""
        SELECT id, title, lang, source, gen_model, content_hash, times_used,
               created_at, last_used_at, reusable, used_as_original,
               parent_text_id, (spin_formula IS NOT NULL) AS has_spin,
               {body_col} AS body_out, {{rank}}
        FROM texts
        WHERE {{where}}
        ORDER BY {{order}}
        LIMIT :limit
    """


async def _enrich(session: AsyncSession, rows: list[dict]) -> list[dict]:
    """Дополнить тексты данными из text_items (anchor/keyword/link, posted-счётчик)
    и числом спин-вариантов (texts.parent_text_id). 3 батч-запроса по id-шникам
    результата — без коррелированных подзапросов."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    # счётчики постинга: всего айтемов + сколько реально запощено
    cnt = {r["tid"]: r for r in (await session.execute(sql("""
        SELECT text_id AS tid, count(*) AS items,
               count(*) FILTER (WHERE status = 'posted') AS posted
        FROM text_items WHERE text_id = ANY(:ids) GROUP BY text_id
    """), {"ids": ids})).mappings().all()}
    # последний anchor/keyword/link по тексту (репрезентативно для gen_per_post/спинов)
    meta = {r["tid"]: r for r in (await session.execute(sql("""
        SELECT DISTINCT ON (text_id) text_id AS tid, link_anchor AS anchor,
               link_url AS link, gen_row->>'keyword' AS keyword
        FROM text_items WHERE text_id = ANY(:ids) ORDER BY text_id, id DESC
    """), {"ids": ids})).mappings().all()}
    # число спин-вариантов (детей-оригинала)
    spins = {r["pid"]: r["n"] for r in (await session.execute(sql("""
        SELECT parent_text_id AS pid, count(*) AS n
        FROM texts WHERE parent_text_id = ANY(:ids) GROUP BY parent_text_id
    """), {"ids": ids})).mappings().all()}
    for r in rows:
        c, m = cnt.get(r["id"]), meta.get(r["id"])
        r["item_count"] = int(c["items"]) if c else 0
        r["posted_count"] = int(c["posted"]) if c else 0
        r["anchor"] = m["anchor"] if m else None
        r["keyword"] = m["keyword"] if m else None
        r["link"] = m["link"] if m else None
        r["spin_count"] = int(spins.get(r["id"], 0))
    return rows


async def search_texts(
    session: AsyncSession, *, q: str | None = None, lang: str | None = None,
    reusable_only: bool = False, limit: int = 50, include_body: bool = False,
) -> list[dict]:
    """Поиск по библиотеке texts: FTS (body_tsv) + нечёткий trgm (title),
    ранжирование ts_rank + similarity. Без q — последние тексты. Фильтры: язык,
    reusable_only (только reuse-кандидаты). Доп. данные (anchor/keyword/link/
    posted/spin_count) — из text_items/children. include_body — для экспорта."""
    limit = max(1, min(limit, 5000))
    q = (q or "").strip()
    body_col = "body" if include_body else f"{_SNIPPET}"
    where = ["archived_at IS NULL", "((:lang)::text IS NULL OR lang = (:lang)::text)"]
    if reusable_only:
        where.append("reusable IS TRUE")
    if q:
        where.append("(body_tsv @@ websearch_to_tsquery('simple', :q) OR title % :q)")
        rank = ("ts_rank(body_tsv, websearch_to_tsquery('simple', :q))"
                " + coalesce(similarity(title, :q), 0) AS rank")
        order = "rank DESC, created_at DESC"
    else:
        rank = "0::float AS rank"
        order = "created_at DESC"
    stmt = sql(_base_select(body_col).format(
        rank=rank, where=" AND ".join(where), order=order))
    rows = [dict(r) for r in (await session.execute(
        stmt, {"q": q, "lang": lang, "limit": limit})).mappings().all()]
    return await _enrich(session, rows)


async def get_text(session: AsyncSession, text_id: int) -> Text | None:
    """Один текст библиотеки целиком (для редактора)."""
    return await session.scalar(select(Text).where(Text.id == text_id))


async def create_texts(session: AsyncSession, rows: list[dict]) -> list[int]:
    """Bulk-вставка тел в texts с возвратом id в порядке входа
    (SQLAlchemy 2.0 insertmanyvalues + RETURNING). rows: список dict с ключами
    body/title/lang/source/content_hash[/gen_model]."""
    if not rows:
        return []
    result = await session.execute(insert(Text).returning(Text.id), rows)
    return [int(r[0]) for r in result.all()]


def _read_minio(storage_key: str | None) -> str:
    if not storage_key:
        return ""
    try:
        raw = storage.get_bytes(settings.MINIO_BUCKET_TEXT_ITEMS, storage_key)
    except StorageError as e:
        log.warning("texts.minio_read_failed", storage_key=storage_key, error=str(e))
        return ""
    from core.text_decode import decode_text
    return decode_text(raw)


async def read_item_body(
    session: AsyncSession, *, text_id: int | None, storage_key: str | None
) -> str:
    """Тело текста: из texts.body (приоритет) либо MinIO storage_key (fallback)."""
    if text_id is not None:
        body = await session.scalar(select(Text.body).where(Text.id == text_id))
        if body is not None:
            return body
    return _read_minio(storage_key)


async def update_text_body(
    session: AsyncSession, text_id: int, *, body: str, title: str | None = None
) -> None:
    """Обновить тело (и опц. title) в texts. body_tsv пересчитается БД-ой
    автоматически (generated STORED)."""
    vals: dict = {"body": body}
    if title is not None:
        vals["title"] = title or None
    await session.execute(update(Text).where(Text.id == text_id).values(**vals))
