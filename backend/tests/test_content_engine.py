"""Content Engine fanout+materialize (C1-2)."""

from __future__ import annotations

from sqlalchemy import select, text as sql

from domain.content_engine import fanout_materialize
from infrastructure.db.models import Text, TextItem


async def test_fanout_materialize(db_session):
    s = db_session
    # оригинал со спином
    orig = Text(
        body="<p>Best casino review. Visit now.</p>",
        title="Casino", lang="en", source="generated",
        spin_formula="<p>{Best|Top} casino {review|guide}. {Visit|Open} now.</p>",
        reusable=True, content_hash="orig" + "0" * 60,
    )
    s.add(orig)
    await s.commit()
    rid = (await s.execute(sql(
        "INSERT INTO posting_runs (project_id,name,status,task_type,concurrency,"
        "timeout_seconds,total_texts,priority,posting_method,spread_days,content_source,"
        "run_mode,created_at,updated_at) VALUES (3,'CE POC','draft','post',25,30,0,"
        "'normal','auto',0,'csv_campaign','manual',now(),now()) RETURNING id"
    ))).scalar_one()
    await s.commit()
    try:
        placements = [
            {"link": "https://nawal.mx/a", "anchor": "Nawal"},
            {"link": "https://nawal.mx/b", "anchor": "Casino"},
            {"link": "https://other.com/c", "anchor": "go"},
        ]
        ids = await fanout_materialize(
            s, run_id=rid, project_id=3, original=orig, placements=placements)
        assert len(ids) == 3

        items = (await s.execute(
            select(TextItem).where(TextItem.id.in_(ids)).order_by(TextItem.id))).scalars().all()
        for it, p in zip(items, placements, strict=True):
            assert it.text_id is not None and it.text_id != orig.id  # своя texts-строка
            assert it.link_url == p["link"]
            # вариант = spin_variant, parent = оригинал
            v = await s.scalar(select(Text).where(Text.id == it.text_id))
            assert v.source == "spin_variant" and v.parent_text_id == orig.id
            assert v.reusable is False
            assert p["link"] in v.body          # ссылка инжектнута
            assert "{" not in v.body            # спин расшит

        # target_domain нормализован из ссылки
        assert items[0].target_domain == "nawal.mx"
        assert items[2].target_domain == "other.com"

        # оригиналу — счётчики reuse
        await s.refresh(orig)
        assert orig.times_used == 3 and orig.used_as_original is True
    finally:
        await s.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": rid})
        await s.execute(sql("DELETE FROM texts WHERE parent_text_id=:p OR id=:p"), {"p": orig.id})
        await s.commit()
