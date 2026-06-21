"""Content Engine: оркестрация spin_fanout-рана (C1, без AI).

create_spin_run: создаёт ран + M оригиналов-спинтаксов (texts, reusable) +
кладёт в gen_params список размещений. auto → сразу fanout+постинг; manual →
статус READY (ждёт ручного Start после ревью оригиналов).

start_spin_run: распределяет размещения по оригиналам (round-robin),
fanout_materialize по каждому → N pending text_items, drip по spread_days,
ставит run в очередь Celery.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text as sql, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import WriteSession
from domain.content_engine.service import fanout_materialize
from infrastructure.db.models import PostingRun, PostingRunStatus, Text, TextItem

log = structlog.get_logger(__name__)


def _distribute(placements: list[dict], main_ids: list[int]) -> dict[int, list[dict]]:
    """Round-robin: размещение i → оригинал main_ids[i % M]."""
    buckets: dict[int, list[dict]] = {mid: [] for mid in main_ids}
    if not main_ids:
        return buckets
    for i, p in enumerate(placements):
        buckets[main_ids[i % len(main_ids)]].append(p)
    return buckets


def _expand_placements(rows: list[dict]) -> list[dict]:
    """rows [{link,anchor,count?}] → плоский список размещений (count копий)."""
    out: list[dict] = []
    for r in rows:
        link = (r.get("link") or "").strip()
        if not link:
            continue
        anchor = (r.get("anchor") or "").strip()
        try:
            n = max(1, int(r.get("count") or 1))
        except (TypeError, ValueError):
            n = 1
        out.extend([{"link": link, "anchor": anchor}] * n)
    return out


async def create_spin_run(
    session: AsyncSession,
    *,
    project_id: int,
    creator_id: int | None,
    name: str,
    originals: list[dict],          # [{spintax, title?, lang?}]
    rows: list[dict],               # [{link, anchor, count?}]
    run_mode: str = "manual",
    scheduled_for=None,
    spread_days: int = 0,
    proxy_selector: str | None = None,
    posting_method: str = "auto",
    priority: str = "normal",
    site_langs: list[str] | None = None,
    site_tlds: list[str] | None = None,
) -> PostingRun:
    placements = _expand_placements(rows)
    if not originals:
        raise ValueError("нет оригиналов (spintax)")
    if not placements:
        raise ValueError("нет размещений (link/anchor/count)")

    # 1) оригиналы → texts (spintax в body И spin_formula; правится в редакторе)
    main_ids: list[int] = []
    for o in originals:
        spintax = (o.get("spintax") or "").strip()
        if not spintax:
            continue
        t = Text(
            body=spintax, spin_formula=spintax,
            title=(o.get("title") or None), lang=(o.get("lang") or None),
            source="human", reusable=True,
            content_hash=hashlib.sha256(spintax.encode("utf-8")).hexdigest(),
        )
        session.add(t)
        await session.flush()
        main_ids.append(t.id)
    if not main_ids:
        raise ValueError("все оригиналы пустые")

    # 2) ран
    run = PostingRun(
        project_id=project_id, created_by=creator_id, name=name.strip(),
        status=PostingRunStatus.UNPACKING.value,
        task_type="post", content_source="spin_fanout",
        content_mode="gen_per_row", run_mode=run_mode,
        scheduled_for=scheduled_for, spread_days=spread_days,
        proxy_selector=proxy_selector, posting_method=posting_method, priority=priority,
        gen_params={"main_text_ids": main_ids, "placements": placements,
                    "distribution": "round_robin",
                    **({"site_langs": site_langs} if site_langs else {}),
                    **({"site_tlds": site_tlds} if site_tlds else {})},
    )
    session.add(run)
    await session.commit()

    if run_mode == "auto":
        await start_spin_run(run.id)
    else:
        # ждём ручного Start (ревью оригиналов) — статус READY
        async with WriteSession() as s:
            await s.execute(update(PostingRun).where(PostingRun.id == run.id)
                            .values(status=PostingRunStatus.READY.value,
                                    total_texts=len(placements)))
            await s.commit()
    return run


async def start_spin_run(run_id: int) -> dict:
    """Раскладка размещений по оригиналам + fanout + постановка в очередь."""
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        gp = run.gen_params or {}
        main_ids: list[int] = gp.get("main_text_ids") or []
        placements: list[dict] = gp.get("placements") or []
        if not main_ids or not placements:
            return {"ok": False, "error": "nothing to fanout"}
        # идемпотентность: уже разложен?
        existing = await s.scalar(select(TextItem.id).where(
            TextItem.posting_run_id == run_id).limit(1))
        if existing:
            return {"ok": True, "status": "already_started"}
        originals = {t.id: t for t in (await s.scalars(
            select(Text).where(Text.id.in_(main_ids)))).all()}

    buckets = _distribute(placements, main_ids)

    total = 0
    for mid, pls in buckets.items():
        if not pls:
            continue
        async with WriteSession() as s:
            orig = await s.scalar(select(Text).where(Text.id == mid))
            await fanout_materialize(
                s, run_id=run_id, project_id=run.project_id,
                original=orig, placements=pls)
        total += len(pls)

    # drip по spread_days (как в unpack) + статус → queued + Celery
    async with WriteSession() as s:
        if run.spread_days and run.spread_days > 0:
            now_ts = datetime.now(UTC)
            ws = run.scheduled_for if (run.scheduled_for and run.scheduled_for > now_ts) else now_ts
            await s.execute(sql("""
                WITH o AS (SELECT id, (row_number() OVER (ORDER BY id)-1)::float AS rn,
                                  GREATEST(count(*) OVER ()-1,1)::float AS denom
                           FROM text_items WHERE posting_run_id=:rid)
                UPDATE text_items t SET not_before=(:ws)::timestamptz
                       + make_interval(secs=>(o.rn/o.denom)*:win)
                FROM o WHERE t.id=o.id
            """), {"rid": run_id, "ws": ws, "win": run.spread_days * 86400})
        await s.execute(update(PostingRun).where(PostingRun.id == run_id)
                        .values(status=PostingRunStatus.QUEUED.value, total_texts=total))
        await s.commit()

    from core.celery_app import celery_app
    from infrastructure.db.models import CELERY_PRIORITY_MAP
    celery_app.send_task("postings.run_posting", args=[run_id],
                         priority=CELERY_PRIORITY_MAP.get(run.priority, 5))
    log.info("content_engine.spin_run.started", run_id=run_id, placements=total)
    return {"ok": True, "status": "queued", "placements": total}
