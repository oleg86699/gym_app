"""Per-item постинг/репост по кнопке (вне общего run-цикла): routing, guards,
claim, exclude-site при репосте. Сам постинг (_post_one_item / process_link_item)
мокаем — проверяем именно обвязку standalone-функции."""
from __future__ import annotations

import workers.celery.posting as wp
from core.db import WriteSession
from infrastructure.db.models import PostingRun, Project, TextItem
from infrastructure.db.models.posting import TextItemStatus
from sqlalchemy import text, update


async def _mk(s, *, status, with_text=True, site_id=None, task_type="post"):
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="PITEM TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()
    tid = None
    if with_text:
        tid = await s.scalar(text(
            "INSERT INTO texts(body, source, content_hash, reusable, created_at) "
            "VALUES ('<p>x</p>', 'generated', :h, true, now()) RETURNING id"),
            {"h": f"{proj.id:064d}"})  # уникальный hash per проект
    run = PostingRun(project_id=proj.id, name="PITEM", status="running",
                     task_type=task_type)
    s.add(run)
    await s.flush()
    item = TextItem(
        posting_run_id=run.id, project_id=proj.id, original_filename="t",
        content_hash=f"{run.id:064d}", byte_size=5, status=status, text_id=tid,
        site_id=site_id, title="T",
        posted_url=("http://x/p" if status == TextItemStatus.POSTED.value else None))
    s.add(item)
    await s.commit()
    return proj, run, item


async def _cleanup(s, proj_id):
    tids = [r[0] for r in (await s.execute(text(
        "SELECT text_id FROM text_items WHERE project_id=:p AND text_id IS NOT NULL"),
        {"p": proj_id})).all()]
    await s.execute(text("DELETE FROM text_items WHERE project_id=:p"), {"p": proj_id})
    if tids:
        await s.execute(text("DELETE FROM texts WHERE id = ANY(:i)"), {"i": tids})
    await s.execute(text("DELETE FROM posting_runs WHERE project_id=:p"), {"p": proj_id})
    await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj_id})
    await s.commit()


async def test_post_pending_with_text_calls_poster(db_session, monkeypatch):
    s = db_session
    proj, run, item = await _mk(s, status=TextItemStatus.PENDING.value)
    cap: dict = {}

    async def fake_post_one(*, item, run, poster, client_pool, registry, semaphore,
                            global_limit):
        cap["item_id"] = item.id
        async with WriteSession() as s2:
            await s2.execute(update(TextItem).where(TextItem.id == item.id)
                             .values(status=TextItemStatus.POSTED.value))
            await s2.commit()

    monkeypatch.setattr(wp, "_post_one_item", fake_post_one)
    try:
        res = await wp._post_one_item_standalone(item.id, is_repost=False)
        assert cap["item_id"] == item.id
        assert res["status"] == TextItemStatus.POSTED.value
    finally:
        await _cleanup(s, proj.id)


async def test_post_no_text_blocked(db_session, monkeypatch):
    s = db_session
    proj, run, item = await _mk(s, status=TextItemStatus.PENDING.value, with_text=False)
    called = {"n": 0}

    async def fake(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(wp, "_post_one_item", fake)
    try:
        res = await wp._post_one_item_standalone(item.id, is_repost=False)
        assert res["status"] == "no_text" and called["n"] == 0
    finally:
        await _cleanup(s, proj.id)


async def test_repost_resets_and_excludes_old_site(db_session, monkeypatch):
    s = db_session
    proj, run, item = await _mk(s, status=TextItemStatus.POSTED.value, site_id=777)
    cap: dict = {}

    async def fake_post_one(*, item, run, poster, client_pool, registry, semaphore,
                            global_limit):
        cap["registry"] = registry
        cap["status_at_call"] = item.status
        cap["site_at_call"] = item.site_id

    monkeypatch.setattr(wp, "_post_one_item", fake_post_one)
    try:
        await wp._post_one_item_standalone(item.id, is_repost=True)
        assert 777 in cap["registry"]._exhausted          # старый сайт исключён
        assert cap["status_at_call"] == TextItemStatus.POSTING.value  # claim
        assert cap["site_at_call"] is None                # результат сброшен
    finally:
        await _cleanup(s, proj.id)


async def test_repost_non_posted_blocked(db_session, monkeypatch):
    s = db_session
    proj, run, item = await _mk(s, status=TextItemStatus.PENDING.value)

    async def fake(*a, **k):
        raise AssertionError("постинг не должен вызваться")

    monkeypatch.setattr(wp, "_post_one_item", fake)
    try:
        res = await wp._post_one_item_standalone(item.id, is_repost=True)
        assert res["status"] == "not_posted"
    finally:
        await _cleanup(s, proj.id)


async def test_link_repost_routes_to_process_link_item(db_session, monkeypatch):
    import domain.wp_links as wl
    s = db_session
    proj, run, item = await _mk(s, status=TextItemStatus.POSTED.value,
                                with_text=False, site_id=888,
                                task_type="sitewide_link")
    cap: dict = {}

    async def fake_pli(item_id, *, used_sites=None, proxy_urls=None, **k):
        cap["used_sites"] = used_sites
        return {"ok": True, "status": "placed"}

    monkeypatch.setattr(wl, "process_link_item", fake_pli)
    try:
        res = await wp._post_one_item_standalone(item.id, is_repost=True)
        assert res["status"] == "placed"
        assert 888 in cap["used_sites"]                   # старый сайт исключён
    finally:
        await _cleanup(s, proj.id)
