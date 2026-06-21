"""B1: единая библиотека texts — bulk-insert, чтение, правка, fallback."""

from __future__ import annotations

from sqlalchemy import text as sql

from domain.texts import create_texts, read_item_body, search_texts, update_text_body
from infrastructure.db.models import PostingRun, Project, TextItem


async def test_create_read_update_texts(db_session):
    s = db_session
    ids = await create_texts(s, [
        {"body": "первое тело", "title": "T1", "lang": "ru",
         "source": "human", "content_hash": "t1" + "0" * 62},
        {"body": "second body", "title": "T2", "lang": "en",
         "source": "human", "content_hash": "t2" + "0" * 62},
    ])
    await s.commit()
    try:
        # порядок id сохраняется (insertmanyvalues)
        assert len(ids) == 2 and ids[0] < ids[1]

        # чтение из texts (приоритет над MinIO)
        b0 = await read_item_body(s, text_id=ids[0], storage_key=None)
        assert b0 == "первое тело"

        # fallback: нет text_id и нет storage_key → пусто
        assert await read_item_body(s, text_id=None, storage_key=None) == ""

        # правка тела → body_tsv пересчитывается БД-ой
        await update_text_body(s, ids[1], body="updated content here")
        await s.commit()
        assert await read_item_body(s, text_id=ids[1], storage_key=None) == "updated content here"
        tsv = await s.scalar(sql("SELECT body_tsv::text FROM texts WHERE id=:i"), {"i": ids[1]})
        assert "updated" in tsv and "content" in tsv
    finally:
        await s.execute(sql("DELETE FROM texts WHERE id = ANY(:ids)"), {"ids": ids})
        await s.commit()


async def test_search_enriched_and_reusable_filter(db_session):
    """search_texts: обогащение из text_items (anchor/keyword/posted) + spin_count
    из детей + фильтр reusable_only."""
    s = db_session
    ids = await create_texts(s, [
        {"body": "<p>zxqvlibtest review alpha</p>", "title": "Alpha", "lang": "en",
         "source": "generated", "content_hash": "tx" + "a" * 62,
         "reusable": True, "spin_formula": "{a|b}"},
        {"body": "<p>zxqvlibtest review beta</p>", "title": "Beta", "lang": "en",
         "source": "generated", "content_hash": "tx" + "b" * 62, "reusable": False},
    ])
    child = await create_texts(s, [
        {"body": "<p>zxqvlibtest spin gamma</p>", "title": "Gamma", "lang": "en",
         "source": "spin_variant", "content_hash": "tx" + "c" * 62,
         "parent_text_id": ids[0]},
    ])
    await s.commit()
    orig_id, plain_id, child_id = ids[0], ids[1], child[0]
    owner = await s.scalar(sql("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="TX TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()
    run = PostingRun(project_id=proj.id, name="TX", status="done",
                     task_type="post", content_source="csv_campaign")
    s.add(run)
    await s.flush()
    s.add(TextItem(posting_run_id=run.id, project_id=proj.id, text_id=plain_id,
                   original_filename="f", content_hash="ti" + "1" * 62, byte_size=5,
                   status="posted", link_anchor="best casino",
                   link_url="https://x.test/", gen_row={"keyword": "casino kw"}))
    await s.commit()
    try:
        by_id = {r["id"]: r for r in await search_texts(s, q="zxqvlibtest", limit=50)}
        # обогащение из text_items
        assert by_id[plain_id]["anchor"] == "best casino"
        assert by_id[plain_id]["keyword"] == "casino kw"
        assert by_id[plain_id]["posted_count"] == 1
        assert by_id[plain_id]["item_count"] == 1
        # spin_count оригинала = 1 (ребёнок), has_spin по spin_formula
        assert by_id[orig_id]["spin_count"] == 1
        assert by_id[orig_id]["has_spin"] is True
        # reusable_only — только reusable=true
        rids = {r["id"] for r in await search_texts(s, q="zxqvlibtest", reusable_only=True, limit=50)}
        assert orig_id in rids and plain_id not in rids
    finally:
        await s.execute(sql("DELETE FROM text_items WHERE project_id=:p"), {"p": proj.id})
        await s.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
        await s.execute(sql("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.execute(sql("DELETE FROM texts WHERE id = ANY(:i)"),
                        {"i": [orig_id, plain_id, child_id]})
        await s.commit()
