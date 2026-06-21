"""/admin/api/texts — библиотека текстов: поиск (FTS + trgm), редактор, экспорт.

Страница /texts по умолчанию только для super_admin (require_page_access);
остальным открывают вручную через /pages."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import require_page_access
from core.db import get_db_read, get_db_write
from domain.texts import get_text, search_texts, update_text_body
from infrastructure.db.models import AdminUser

router = APIRouter(prefix="/texts", tags=["texts"])

_PAGE = require_page_access("/texts")


class TextSearchRow(BaseModel):
    id: int
    title: str | None
    lang: str | None
    source: str
    gen_model: str | None = None
    content_hash: str
    times_used: int
    posted_count: int = 0       # сколько раз реально запощен (text_items.posted)
    item_count: int = 0         # сколько айтемов ссылается
    created_at: datetime
    last_used_at: datetime | None = None
    snippet: str | None = None
    rank: float = 0.0
    # reuse / спин / происхождение
    reusable: bool = False
    has_spin: bool = False
    used_as_original: bool = False
    parent_text_id: int | None = None
    spin_count: int = 0         # число спин-вариантов (детей оригинала)
    # gen-контекст (из text_items, репрезентативно)
    anchor: str | None = None
    keyword: str | None = None
    link: str | None = None


class TextDetail(BaseModel):
    id: int
    title: str | None
    body: str
    lang: str | None
    source: str
    reusable: bool
    spin_formula: str | None
    parent_text_id: int | None
    times_used: int
    created_at: datetime


class TextUpdate(BaseModel):
    title: str | None = None
    body: str


def _row(d: dict) -> TextSearchRow:
    # body_out (snippet) приходит под этим ключом из сервиса
    return TextSearchRow(snippet=d.get("body_out"), **{
        k: v for k, v in d.items() if k != "body_out"})


@router.get("", response_model=list[TextSearchRow])
async def search_library(
    q: str | None = Query(default=None, max_length=500),
    lang: str | None = Query(default=None, max_length=10),
    reusable_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    _: AdminUser = Depends(_PAGE),
    session: AsyncSession = Depends(get_db_read),
) -> list[TextSearchRow]:
    rows = await search_texts(session, q=q, lang=lang,
                              reusable_only=reusable_only, limit=limit)
    return [_row(r) for r in rows]


@router.get("/export.csv")
async def export_library(
    q: str | None = Query(default=None, max_length=500),
    lang: str | None = Query(default=None, max_length=10),
    reusable_only: bool = Query(default=False),
    with_body: bool = Query(default=False),
    limit: int = Query(default=5000, ge=1, le=5000),
    _: AdminUser = Depends(_PAGE),
    session: AsyncSession = Depends(get_db_read),
) -> StreamingResponse:
    """CSV-выгрузка отфильтрованной библиотеки. with_body=true добавляет колонку
    body (полное тело) — для бэкапа/переноса; иначе только метаданные."""
    rows = await search_texts(session, q=q, lang=lang, reusable_only=reusable_only,
                              limit=limit, include_body=with_body)
    cols = ["id", "title", "lang", "source", "reusable", "has_spin", "spin_count",
            "anchor", "keyword", "link", "times_used", "posted_count",
            "item_count", "created_at"]
    if with_body:
        cols.append("body")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for d in rows:
        d["body"] = d.get("body_out") if with_body else None
        w.writerow([d.get(c) for c in cols])
    buf.seek(0)
    fname = "texts-reuse.csv" if reusable_only else "texts.csv"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/{text_id}", response_model=TextDetail)
async def get_one(
    text_id: int,
    _: AdminUser = Depends(_PAGE),
    session: AsyncSession = Depends(get_db_read),
) -> TextDetail:
    t = await get_text(session, text_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Text not found")
    return TextDetail(
        id=t.id, title=t.title, body=t.body, lang=t.lang, source=t.source,
        reusable=t.reusable, spin_formula=t.spin_formula,
        parent_text_id=t.parent_text_id, times_used=t.times_used,
        created_at=t.created_at)


@router.put("/{text_id}", response_model=TextDetail)
async def update_one(
    text_id: int,
    payload: TextUpdate,
    _: AdminUser = Depends(_PAGE),
    session: AsyncSession = Depends(get_db_write),
) -> TextDetail:
    t = await get_text(session, text_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Text not found")
    await update_text_body(session, text_id, body=payload.body, title=payload.title)
    await session.commit()
    t = await get_text(session, text_id)
    assert t is not None
    return TextDetail(
        id=t.id, title=t.title, body=t.body, lang=t.lang, source=t.source,
        reusable=t.reusable, spin_formula=t.spin_formula,
        parent_text_id=t.parent_text_id, times_used=t.times_used,
        created_at=t.created_at)
