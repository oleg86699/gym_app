"""Пер-айтем (ре)генерация текста: per_post (AI), gen_per_row оригинал (AI →
тело оригинала + сброс spin_formula), gen_per_row спин (переспин). AI-вызов
(_gen) и pick_model мокаем."""
from __future__ import annotations

import domain.content_engine.campaign as camp
from core.db import WriteSession
from infrastructure.db.models import PostingRun, Project, Text, TextItem
from infrastructure.db.models.posting import TextItemStatus
from sqlalchemy import select, text


class _FakeModel:
    model_id = "fake-gpt"


async def _fake_pick_model(s, *, purpose, model_pk=None):
    return _FakeModel() if purpose == "content" else None


def _patch_ai(monkeypatch, gen_body="<p>NEW текст</p>"):
    async def fake_gen(model, prompt):
        return gen_body
    monkeypatch.setattr(camp, "_gen", fake_gen)
    monkeypatch.setattr(camp, "pick_model", _fake_pick_model)


async def _mk_project(s):
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="GENITEM TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()
    return proj


async def _mk_text(s, proj_id, body, *, spin_formula=None, suffix="o"):
    return await s.scalar(text(
        "INSERT INTO texts(body, source, content_hash, spin_formula, reusable, created_at) "
        "VALUES (:b,'generated',:h,:sf,true,now()) RETURNING id"),
        {"b": body, "h": f"{proj_id:063d}{suffix}", "sf": spin_formula})


async def _cleanup(s, proj_id, extra_tids=()):
    tids = set(t for t in extra_tids if t)
    tids |= {r[0] for r in (await s.execute(text(
        "SELECT DISTINCT text_id FROM text_items WHERE project_id=:p AND text_id IS NOT NULL"),
        {"p": proj_id})).all()}
    await s.execute(text("DELETE FROM text_items WHERE project_id=:p"), {"p": proj_id})
    if tids:
        await s.execute(text("DELETE FROM texts WHERE id = ANY(:i)"), {"i": list(tids)})
    await s.execute(text("DELETE FROM posting_runs WHERE project_id=:p"), {"p": proj_id})
    await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj_id})
    await s.commit()


async def test_per_post_regenerate(db_session, monkeypatch):
    s = db_session
    _patch_ai(monkeypatch)
    proj = await _mk_project(s)
    old_tid = await _mk_text(s, proj.id, "<p>old</p>")
    run = PostingRun(project_id=proj.id, name="G", status="ready",
                     content_source="csv_campaign", content_mode="gen_per_post",
                     gen_params={"language": "English"})
    s.add(run)
    await s.flush()
    item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                    content_hash=f"{run.id:064d}", byte_size=3, status="pending",
                    text_id=old_tid, link_url="https://x/", link_anchor="a",
                    gen_row={"keyword": "casino", "link": "https://x/", "anchor": "a"})
    s.add(item)
    await s.commit()
    try:
        res = await camp.generate_item(item.id, regenerate=True)
        assert res["ok"] and res["kind"] == "per_post"
        async with WriteSession() as s2:
            it = await s2.scalar(select(TextItem).where(TextItem.id == item.id))
            assert it.status == TextItemStatus.PENDING.value
            assert it.text_id != old_tid                 # новый Text
            body = await s2.scalar(select(Text.body).where(Text.id == it.text_id))
            assert "NEW" in body
    finally:
        await _cleanup(s, proj.id, extra_tids=[old_tid])


async def test_per_post_generate_requires_no_text(db_session, monkeypatch):
    """Без текста — генерируем; повторный generate (текст есть) → already_has_text."""
    s = db_session
    _patch_ai(monkeypatch)
    proj = await _mk_project(s)
    run = PostingRun(project_id=proj.id, name="G", status="ready",
                     content_source="csv_campaign", content_mode="gen_per_post",
                     gen_params={"language": "English"})
    s.add(run)
    await s.flush()
    item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                    content_hash=f"{run.id:064d}", byte_size=0, status="pending",
                    text_id=None, gen_row={"keyword": "k", "link": "https://x/", "anchor": "a"})
    s.add(item)
    await s.commit()
    try:
        res = await camp.generate_item(item.id, regenerate=False)
        assert res["ok"]
        # второй generate без regenerate — текст уже есть
        res2 = await camp.generate_item(item.id, regenerate=False)
        assert res2["status"] == "already_has_text"
    finally:
        await _cleanup(s, proj.id)


async def test_gen_per_row_original_updates_orig_and_resets_spin(db_session, monkeypatch):
    s = db_session
    _patch_ai(monkeypatch, gen_body="<p>fresh original</p>")
    proj = await _mk_project(s)
    orig_id = await _mk_text(s, proj.id, "<p>orig</p>", spin_formula="<p>{a|b}</p>")
    run = PostingRun(project_id=proj.id, name="G", status="ready",
                     content_source="csv_campaign", content_mode="gen_per_row",
                     gen_params={"language": "English"})
    s.add(run)
    await s.flush()
    orig_item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="o",
                         content_hash=f"{run.id:063d}o", byte_size=3, status="pending",
                         text_id=orig_id, link_url="https://x/", link_anchor="a",
                         gen_row={"keyword": "k", "link": "https://x/", "anchor": "a"})
    s.add(orig_item)
    await s.flush()
    # привязываем группу к этому оригинал-айтему
    run.gen_params = {"language": "English", "fanout_groups": [{
        "original_item_id": orig_item.id, "spin_item_ids": [],
        "text_id": orig_id, "link": "https://x/", "anchor": "a"}]}
    await s.commit()
    try:
        res = await camp.generate_item(orig_item.id, regenerate=True)
        assert res["ok"] and res["kind"] == "gen_per_row_original"
        async with WriteSession() as s2:
            orig = await s2.scalar(select(Text).where(Text.id == orig_id))
            assert "fresh original" in orig.body      # тело оригинала обновлено
            assert orig.spin_formula is None          # spin_formula сброшен
    finally:
        await _cleanup(s, proj.id, extra_tids=[orig_id])


async def test_gen_per_row_spin_respins_without_ai(db_session, monkeypatch):
    s = db_session
    _patch_ai(monkeypatch)  # _gen не должен вызваться (spin_formula есть)
    called = {"gen": 0}
    orig_gen = camp._gen

    async def counting_gen(model, prompt):
        called["gen"] += 1
        return await orig_gen(model, prompt)
    monkeypatch.setattr(camp, "_gen", counting_gen)

    proj = await _mk_project(s)
    orig_id = await _mk_text(s, proj.id, "<p>orig</p>", spin_formula="<p>{one|two}</p>")
    run = PostingRun(project_id=proj.id, name="G", status="ready",
                     content_source="csv_campaign", content_mode="gen_per_row",
                     gen_params={"language": "English"})
    s.add(run)
    await s.flush()
    spin_item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="(спин)",
                         content_hash=f"{run.id:063d}s", byte_size=0, status="pending",
                         text_id=None, link_url="https://x/", link_anchor="a",
                         gen_row={"keyword": "k", "link": "https://x/", "anchor": "a"})
    s.add(spin_item)
    await s.flush()
    run.gen_params = {"language": "English", "fanout_groups": [{
        "original_item_id": 0, "spin_item_ids": [spin_item.id],
        "text_id": orig_id, "link": "https://x/", "anchor": "a"}]}
    await s.commit()
    try:
        res = await camp.generate_item(spin_item.id, regenerate=False)
        assert res["ok"] and res["kind"] == "gen_per_row_spin"
        assert called["gen"] == 0                      # переспин без AI
        async with WriteSession() as s2:
            it = await s2.scalar(select(TextItem).where(TextItem.id == spin_item.id))
            assert it.text_id is not None and it.status == TextItemStatus.PENDING.value
            body = await s2.scalar(select(Text.body).where(Text.id == it.text_id))
            assert "https://x/" in body                # ссылка инжектнута
    finally:
        await _cleanup(s, proj.id, extra_tids=[orig_id])


async def test_generate_rejects_non_gen_run(db_session, monkeypatch):
    s = db_session
    proj = await _mk_project(s)
    run = PostingRun(project_id=proj.id, name="P", status="ready",
                     content_source="upload_txt", task_type="post")
    s.add(run)
    await s.flush()
    item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                    content_hash=f"{run.id:064d}", byte_size=0, status="pending")
    s.add(item)
    await s.commit()
    try:
        res = await camp.generate_item(item.id, regenerate=False)
        assert res["status"] == "not_a_gen_run"
    finally:
        await _cleanup(s, proj.id)
