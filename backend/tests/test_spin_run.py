"""Оркестрация spin_fanout-рана: create (manual) + распределение (C1-4)."""

from __future__ import annotations

from sqlalchemy import select, text as sql

from domain.content_engine import create_spin_run
from domain.content_engine.runs import _distribute, _expand_placements
from infrastructure.db.models import PostingRun, Text


def test_expand_placements_count():
    rows = [{"link": "https://a.com/", "anchor": "A", "count": 3},
            {"link": "https://b.com/", "anchor": "B"}]
    pl = _expand_placements(rows)
    assert len(pl) == 4  # 3 + 1
    assert sum(1 for p in pl if p["link"] == "https://a.com/") == 3


def test_distribute_round_robin():
    pls = [{"link": f"https://x{i}.com/"} for i in range(7)]
    buckets = _distribute(pls, [10, 20, 30])
    # 7 размещений по 3 оригиналам → 3/2/2
    assert sorted(len(v) for v in buckets.values()) == [2, 2, 3]
    assert sum(len(v) for v in buckets.values()) == 7


async def test_create_spin_run_manual(db_session):
    s = db_session
    run = await create_spin_run(
        s, project_id=3, creator_id=None, name="SPIN POC",
        originals=[
            {"spintax": "<p>{Best|Top} casino {review|guide}.</p>", "lang": "en"},
            {"spintax": "<p>{Great|Nice} {bonus|offer} here.</p>", "lang": "en"},
        ],
        rows=[{"link": "https://nawal.mx/a", "anchor": "Nawal", "count": 3}],
        run_mode="manual",
    )
    try:
        # manual → READY (ждёт ручного Start), не запостился
        await s.refresh(run)
        assert run.status == "ready"
        assert run.content_source == "spin_fanout" and run.run_mode == "manual"
        # 2 оригинала в texts (reusable, спинтакс в body+spin_formula)
        mids = run.gen_params["main_text_ids"]
        assert len(mids) == 2
        origs = (await s.execute(select(Text).where(Text.id.in_(mids)))).scalars().all()
        for o in origs:
            assert o.reusable is True and o.spin_formula and "{" in o.body
        # placements развёрнуты по count
        assert len(run.gen_params["placements"]) == 3
        assert run.total_texts == 3
    finally:
        mids = run.gen_params["main_text_ids"]
        await s.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
        await s.execute(sql("DELETE FROM texts WHERE id = ANY(:ids)"), {"ids": mids})
        await s.commit()
