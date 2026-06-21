"""Reuse-движок (C3): под каждую задачу (link, anchor) берём ПЕРЕИСПОЛЬЗУЕМЫЙ
оригинал из библиотеки (reusable + есть spin_formula + не исчерпан лимит),
расшиваем его спин в новый уникальный вариант и инжектим ссылку — через общий
fanout_materialize (он же бампит times_used и линкует parent_text_id).

Критерий пула (спека C3):
    reusable = TRUE  AND  spin_formula IS NOT NULL  AND  times_used < MAX_SPIN_REUSE
Каждый оригинал отдаёт не более (MAX_SPIN_REUSE - times_used) размещений за раз.
"""

from __future__ import annotations

import os

import structlog
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from domain.content_engine.service import fanout_materialize
from infrastructure.db.models import Text

log = structlog.get_logger(__name__)

# Сколько раз максимум переиспользуем один reusable-оригинал (крутилка).
MAX_SPIN_REUSE = int(os.getenv("MAX_SPIN_REUSE", "50"))


def _expand(tasks: list[dict]) -> list[dict]:
    """[{link, anchor, count}] → плоский список размещений {link, anchor} × count."""
    flat: list[dict] = []
    for t in tasks:
        link = (t.get("link") or "").strip()
        anchor = (t.get("anchor") or "").strip()
        if not link:
            continue
        try:
            cnt = int(t.get("count") or 1)
        except (TypeError, ValueError):
            cnt = 1
        for _ in range(max(1, cnt)):
            flat.append({"link": link, "anchor": anchor})
    return flat


async def generate_reuse_items(
    session: AsyncSession, *, run_id: int, project_id: int,
    tasks: list[dict], lang: str | None = None,
) -> int:
    """Создать text_items reuse-движком. Возвращает кол-во созданных задач.
    0 — если в библиотеке нет подходящих reusable-оригиналов."""
    flat = _expand(tasks)
    if not flat:
        return 0

    # пул переиспользуемых оригиналов с остатком лимита (наименее использованные)
    pool = list((await session.scalars(
        select(Text).where(
            Text.reusable.is_(True),
            Text.spin_formula.isnot(None),
            Text.times_used < MAX_SPIN_REUSE,
            Text.archived_at.is_(None),
            (Text.lang == lang) if lang else true(),
        ).order_by(Text.times_used.asc(), Text.id.asc()).limit(2000)
    )).all())
    if not pool:
        log.warning("reuse.no_reusable_texts", run_id=run_id, lang=lang,
                    note="нужны reusable-тексты со spin_formula и запасом лимита")
        return 0

    # бюджет каждого источника = сколько ещё можно переиспользовать
    budget = {t.id: max(0, MAX_SPIN_REUSE - (t.times_used or 0)) for t in pool}
    by_id = {t.id: t for t in pool}

    # раскидываем размещения round-robin, уважая бюджет источника
    assignments: dict[int, list[dict]] = {}
    order = [t.id for t in pool]
    i = 0
    placed = 0
    for plc in flat:
        # ищем следующий источник с остатком бюджета
        tried = 0
        while tried < len(order) and budget[order[i % len(order)]] <= 0:
            i += 1
            tried += 1
        if tried >= len(order):
            break  # бюджет всего пула исчерпан
        sid = order[i % len(order)]
        budget[sid] -= 1
        i += 1
        assignments.setdefault(sid, []).append(plc)
        placed += 1

    if placed < len(flat):
        log.warning("reuse.budget_exhausted", run_id=run_id,
                    requested=len(flat), placed=placed,
                    note=f"пул reusable исчерпал лимит ({MAX_SPIN_REUSE}/текст)")

    total = 0
    for sid, placements in assignments.items():
        ids = await fanout_materialize(
            session, run_id=run_id, project_id=project_id,
            original=by_id[sid], placements=placements)
        total += len(ids)

    log.info("reuse.generated", run_id=run_id, items=total, sources=len(assignments))
    return total
