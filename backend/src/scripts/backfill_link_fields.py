"""Бэкфилл link_url/link_anchor/target_domain (+ lang) для старых text_items.

Старые txt-посты заливались до Phase A: ссылка/анкор зашиты в теле, но в полях
text_items пусто. Тут парсим тело (analyze_text — тот же движок, что у unpack) и
заполняем поля, чтобы новая таблица в деталях рана показывала Link/Anchor/домен.

Идемпотентно: трогаем только items с link_url IS NULL и непустым text_id.
Запуск:  python -m scripts.backfill_link_fields
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text as sql

from core.db import WriteSession
from domain.text_links import analyze_text, extract_links, normalize_domain

log = structlog.get_logger(__name__)

BATCH = 300


async def _project_domains(s, project_id: int, cache: dict[int, list[str]]) -> list[str]:
    if project_id not in cache:
        rows = (await s.execute(
            sql("SELECT domain FROM project_domains WHERE project_id=:p"),
            {"p": project_id})).all()
        cache[project_id] = [r[0] for r in rows]
    return cache[project_id]


async def run() -> dict:
    cursor = 0
    scanned = filled = no_link = 0
    cache: dict[int, list[str]] = {}
    while True:
        async with WriteSession() as s:
            rows = (await s.execute(sql("""
                SELECT ti.id, ti.project_id, ti.lang, t.body
                FROM text_items ti JOIN texts t ON t.id = ti.text_id
                WHERE ti.link_url IS NULL AND ti.text_id IS NOT NULL AND ti.id > :cur
                ORDER BY ti.id LIMIT :lim
            """), {"cur": cursor, "lim": BATCH})).all()
            if not rows:
                break
            for item_id, project_id, lang, body in rows:
                cursor = item_id
                scanned += 1
                domains = await _project_domains(s, project_id, cache)
                res = analyze_text(body, domains)
                link = res.target_link
                anchor = res.target_anchor
                domain = res.target_domain
                if not link:
                    # ни одна ссылка не «наша» — берём первую ссылку из тела (бэклинк)
                    pairs = extract_links(body)
                    if pairs:
                        link, anchor = pairs[0]
                        domain = normalize_domain(link)
                if not link:
                    no_link += 1
                    continue
                await s.execute(sql("""
                    UPDATE text_items SET link_url=:l, link_anchor=:a, target_domain=:d,
                           lang=COALESCE(lang, :lang)
                    WHERE id=:id
                """), {"l": link[:2000], "a": (anchor or None) and anchor[:500],
                       "d": domain, "lang": (res.lang or lang), "id": item_id})
                filled += 1
            await s.commit()
        log.info("backfill_links.batch", scanned=scanned, filled=filled, cursor=cursor)
    result = {"scanned": scanned, "filled": filled, "no_link": no_link}
    log.info("backfill_links.done", **result)
    return result


if __name__ == "__main__":
    print(asyncio.run(run()))
