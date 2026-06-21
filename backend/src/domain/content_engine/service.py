"""Content Engine: fanout + materialize (C1).

Из одного ОРИГИНАЛА (тело + опц. spin_formula) и списка размещений
(link, anchor[, target_domain]) делаем N материализованных вариантов:
  вариант = inject_link( spin(spin_formula) | original.body , link, anchor )
Каждый вариант → своя строка `texts` (source='spin_variant', parent=оригинал,
reusable=false) + строка `text_items` (text_id=вариант, link/anchor/domain).
Оригиналу: used_as_original=true, times_used += N, last_used_at=now.

Подходит для:
  • mode gen_per_row / spin_fanout — 1 оригинал → count/N размещений;
  • reuse — найденный оригинал → его размещения.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.text_links import inject_link, normalize_domain, spin
from domain.texts import create_texts
from infrastructure.db.models import Text, TextItem, TextItemStatus

log = structlog.get_logger(__name__)


def make_variant(original_body: str, spin_formula: str | None,
                 link: str, anchor: str, rng=None) -> str:
    """Готовое тело одного размещения: расшивка спина (если есть) + инжект ссылки."""
    base = spin(spin_formula, rng=rng) if spin_formula else (original_body or "")
    return inject_link(base, link, anchor or link)


async def fanout_materialize(
    session: AsyncSession,
    *,
    run_id: int,
    project_id: int,
    original: Text,
    placements: list[dict],
    status: str = TextItemStatus.PENDING.value,
    not_before=None,
) -> list[int]:
    """placements: [{link, anchor, target_domain?}, ...]. Возвращает id text_items.
    Материализует по варианту на каждое размещение."""
    if not placements:
        return []
    now = datetime.now(UTC)

    # 1) тела вариантов + строки texts (bulk, с возвратом id в порядке)
    bodies: list[str] = []
    text_rows: list[dict] = []
    for p in placements:
        body = make_variant(original.body, original.spin_formula,
                            p["link"], p.get("anchor") or "")
        bodies.append(body)
        text_rows.append({
            "body": body,
            "title": original.title,
            "lang": original.lang,
            "source": "spin_variant",
            "gen_model": original.gen_model,
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "parent_text_id": original.id,
            "reusable": False,
        })
    variant_ids = await create_texts(session, text_rows)

    # 2) строки text_items, привязанные к своим вариантам
    item_rows: list[dict] = []
    for p, vid, body in zip(placements, variant_ids, bodies, strict=True):
        td = p.get("target_domain") or normalize_domain(p["link"])
        item_rows.append({
            "posting_run_id": run_id,
            "project_id": project_id,
            "text_id": vid,
            "original_filename": (original.title or f"text-{original.id}")[:500],
            "title": original.title,
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "byte_size": len(body.encode("utf-8")),
            "status": status,
            "link_url": p["link"],
            "link_anchor": (p.get("anchor") or None) and p["anchor"][:500],
            "target_domain": td,
            "lang": original.lang,
            "not_before": not_before,
        })
    res = await session.execute(TextItem.__table__.insert().returning(TextItem.id), item_rows)
    item_ids = [int(r[0]) for r in res.all()]

    # 3) оригиналу — счётчики reuse
    await session.execute(
        update(Text).where(Text.id == original.id).values(
            used_as_original=True,
            times_used=Text.times_used + len(placements),
            last_used_at=now,
        )
    )
    await session.commit()
    log.info("content_engine.fanout", original_id=original.id, placements=len(placements))
    return item_ids
