"""Manual «не генерим сразу» + text-гейт постинга:
- _pick_pending_batch(require_text=True) пропускает айтемы без текста (пустые
  gen-айтемы) — постим только готовые;
- /generate-texts: только для gen-задач + идемпотентно (не дублирует айтемы)."""
from __future__ import annotations

import domain.content_engine.campaign as camp
from infrastructure.db.models import PostingRun, Project, Text, TextItem
from sqlalchemy import select, text
from workers.celery.posting import _pick_pending_batch


async def _mk_proj_run(s, **run_kw):
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="MGEN TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()
    run = PostingRun(project_id=proj.id, name="MGEN", **run_kw)
    s.add(run)
    await s.flush()
    return proj, run


async def _cleanup(s, proj_id, extra_tids=()):
    tids = set(t for t in extra_tids if t)
    tids |= {r[0] for r in (await s.execute(text(
        "SELECT text_id FROM text_items WHERE project_id=:p AND text_id IS NOT NULL"),
        {"p": proj_id})).all()}
    await s.execute(text("DELETE FROM text_items WHERE project_id=:p"), {"p": proj_id})
    if tids:
        await s.execute(text("DELETE FROM texts WHERE id = ANY(:i)"), {"i": list(tids)})
    await s.execute(text("DELETE FROM posting_runs WHERE project_id=:p"), {"p": proj_id})
    await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj_id})
    await s.commit()


async def test_create_empty_items_gen_per_post(db_session):
    """gen_per_post: N пустых независимых айтемов (text_id=NULL, gen_row сохранён)."""
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post")
    await s.commit()
    rows = [{"link": "https://a/", "anchor": "x", "count": 3, "keyword": "kw"}]
    total, groups, mains = await camp.create_empty_campaign_items(
        run.id, proj.id, rows, "gen_per_post", "English")
    try:
        assert total == 3 and groups == [] and mains == []
        items = (await s.execute(select(TextItem).where(
            TextItem.posting_run_id == run.id))).scalars().all()
        assert len(items) == 3
        assert all(it.text_id is None for it in items)          # пустые
        assert all((it.gen_row or {}).get("keyword") == "kw" for it in items)
    finally:
        await _cleanup(s, proj.id)


async def test_create_empty_items_gen_per_row(db_session):
    """gen_per_row: плейсхолдер-оригинал (пустое тело) + группа; ВСЕ айтемы
    пустые (включая оригинал)."""
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_row")
    await s.commit()
    rows = [{"link": "https://a/", "anchor": "x", "count": 3, "keyword": "kw"}]
    total, groups, mains = await camp.create_empty_campaign_items(
        run.id, proj.id, rows, "gen_per_row", "English")
    try:
        assert total == 3 and len(groups) == 1 and len(mains) == 1
        g = groups[0]
        assert g["original_item_id"] and len(g["spin_item_ids"]) == 2
        items = (await s.execute(select(TextItem).where(
            TextItem.posting_run_id == run.id))).scalars().all()
        assert len(items) == 3 and all(it.text_id is None for it in items)  # все пустые
        orig = await s.scalar(select(Text).where(Text.id == g["text_id"]))
        assert orig is not None and (orig.body or "") == ""    # пустой плейсхолдер
    finally:
        await _cleanup(s, proj.id, extra_tids=[g["text_id"]])


async def test_pick_pending_batch_skips_textless(db_session):
    s = db_session
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign")
    tid = await s.scalar(text(
        "INSERT INTO texts(body,source,content_hash,reusable,created_at) "
        "VALUES('<p>x</p>','generated',:h,true,now()) RETURNING id"),
        {"h": f"{proj.id:064d}"})
    with_text = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                         content_hash=f"{run.id:063d}a", byte_size=3, status="pending", text_id=tid)
    no_text = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                       content_hash=f"{run.id:063d}b", byte_size=0, status="pending", text_id=None)
    s.add_all([with_text, no_text])
    await s.commit()
    try:
        batch = await _pick_pending_batch(s, run.id, 50, require_text=True)
        ids = {b.id for b in batch}
        assert with_text.id in ids       # с текстом — постим
        assert no_text.id not in ids     # пустой gen-айтем — пропускаем
    finally:
        await _cleanup(s, proj.id)


async def test_apply_drip_not_before(db_session):
    """spread_days → not_before размазан по окну [now, now+N дней] (random)."""
    from datetime import UTC, datetime, timedelta
    s = db_session
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign")
    for i in range(4):
        s.add(TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                       content_hash=f"{run.id:062d}d{i}", byte_size=0, status="pending"))
    await s.commit()
    try:
        await camp.apply_drip_not_before(run.id, spread_days=2)
        nulls = await s.scalar(text(
            "SELECT count(*) FROM text_items WHERE posting_run_id=:r AND not_before IS NULL"),
            {"r": run.id})
        assert nulls == 0                       # всем проставлен not_before
        rng = await s.execute(text(
            "SELECT min(not_before), max(not_before) FROM text_items WHERE posting_run_id=:r"),
            {"r": run.id})
        mn, mx = rng.one()
        now = datetime.now(UTC)
        assert mn >= now - timedelta(minutes=1)
        assert mx <= now + timedelta(days=2, minutes=1)
    finally:
        await _cleanup(s, proj.id)


async def test_streaming_gen_respects_horizon(db_session, monkeypatch):
    """finalize=True (стриминг): генерим только «созревшие» в горизонте; айтемы
    с not_before далеко в будущем НЕ генерятся (drip-генерация по дням)."""
    from datetime import UTC, datetime, timedelta

    async def fake_gen(model, prompt):
        return "<p>x</p>"

    class _M:
        model_id = "m"

    async def fake_pick(s, *, purpose, model_pk=None):
        return _M()

    monkeypatch.setattr(camp, "_gen", fake_gen)
    monkeypatch.setattr(camp, "pick_model", fake_pick)
    s = db_session
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post",
                                   run_mode="auto", gen_params={"language": "English"})
    gr = {"keyword": "k", "link": "https://x/", "anchor": "a"}
    due = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                   content_hash=f"{run.id:062d}h1", byte_size=0, status="pending",
                   text_id=None, not_before=None, gen_row=gr)
    future = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                      content_hash=f"{run.id:062d}h2", byte_size=0, status="pending",
                      text_id=None, not_before=datetime.now(UTC) + timedelta(days=30), gen_row=gr)
    s.add_all([due, future])
    await s.commit()
    due_id, future_id = due.id, future.id
    try:
        await camp.generate_run_items(run.id, finalize=True)
        dt = await s.scalar(text("SELECT text_id FROM text_items WHERE id=:i"), {"i": due_id})
        ft = await s.scalar(text("SELECT text_id FROM text_items WHERE id=:i"), {"i": future_id})
        assert dt is not None        # созревший → сгенерён
        assert ft is None            # будущий → пропущен (drip)
    finally:
        await _cleanup(s, proj.id)


async def test_generate_run_items_finalize_gen_per_row(db_session, monkeypatch):
    """Стриминг finalize=True для gen_per_row: оригинал + спины наполняются
    ФИНАЛЬНЫМ текстом (с инжектом ссылки) — все айтемы сразу постабельны."""
    async def fake_gen(model, prompt):
        return "<p>body {a|b}</p>"

    class _M:
        model_id = "m"

    async def fake_pick(s, *, purpose, model_pk=None):
        return _M()

    monkeypatch.setattr(camp, "_gen", fake_gen)
    monkeypatch.setattr(camp, "pick_model", fake_pick)

    s = db_session
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign",
                                   content_mode="gen_per_row", run_mode="auto")
    await s.commit()
    rows = [{"link": "https://x/", "anchor": "a", "count": 2, "keyword": "k"}]
    total, groups, mains = await camp.create_empty_campaign_items(
        run.id, proj.id, rows, "gen_per_row", "English")
    run.gen_params = {"fanout_groups": groups, "main_text_ids": mains, "language": "English"}
    await s.commit()
    try:
        res = await camp.generate_run_items(run.id, finalize=True)
        assert res["ok"]
        items = (await s.execute(select(TextItem).where(
            TextItem.posting_run_id == run.id))).scalars().all()
        assert len(items) == 2
        assert all(it.text_id is not None for it in items)   # все постабельны
        for it in items:                                     # ссылка инжектнута
            body = await s.scalar(select(Text.body).where(Text.id == it.text_id))
            assert "https://x/" in body
    finally:
        await _cleanup(s, proj.id, extra_tids=[g["text_id"] for g in groups])


async def test_generate_texts_409_when_all_generated(client, auth_headers, db_session):
    """Всё сгенерировано (нет пустых айтемов) → 409."""
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post")
    tid = await s.scalar(text(
        "INSERT INTO texts(body,source,content_hash,reusable,created_at) "
        "VALUES('<p>x</p>','generated',:h,true,now()) RETURNING id"), {"h": f"{proj.id:064d}"})
    item = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                    content_hash=f"{run.id:064d}", byte_size=3, status="pending", text_id=tid)
    s.add(item)
    await s.commit()
    try:
        r = await client.post(f"/admin/api/postings/{run.id}/generate-texts", headers=auth_headers)
        assert r.status_code == 409      # пустых нет → нечего генерить
    finally:
        await _cleanup(s, proj.id)


async def test_generate_texts_rejects_non_gen_run(client, auth_headers, db_session):
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="upload_txt")
    await s.commit()
    try:
        r = await client.post(f"/admin/api/postings/{run.id}/generate-texts", headers=auth_headers)
        assert r.status_code == 409
    finally:
        await _cleanup(s, proj.id)


async def test_fill_campaign_spins_manual(db_session, monkeypatch):
    """Manual «Заполнить спины»: расшивает ГОТОВЫЕ оригиналы в спин-варианты (с
    инжектом ссылки) БЕЗ старта постинга. Ран остаётся READY. Идемпотентно —
    повторный вызов не плодит дубли."""
    async def fake_gen(model, prompt):
        return "{a|b}"          # спинтакс из «отревьюенного» тела

    class _M:
        model_id = "m"

    async def fake_pick(s, *, purpose, model_pk=None):
        return _M()

    monkeypatch.setattr(camp, "_gen", fake_gen)
    monkeypatch.setattr(camp, "pick_model", fake_pick)

    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign",
                                   content_mode="gen_per_row", run_mode="manual")
    await s.commit()
    rows = [{"link": "https://x/", "anchor": "a", "count": 3, "keyword": "k"}]
    total, groups, mains = await camp.create_empty_campaign_items(
        run.id, proj.id, rows, "gen_per_row", "English")
    run.gen_params = {"fanout_groups": groups, "main_text_ids": mains, "language": "English"}
    await s.commit()
    g = groups[0]
    try:
        # симулируем сгенерённый оригинал: тело + оригинал-айтем → text_id оригинала
        await s.execute(text("UPDATE texts SET body='<p>body</p>' WHERE id=:t"),
                        {"t": g["text_id"]})
        await s.execute(text("UPDATE text_items SET text_id=:t WHERE id=:i"),
                        {"t": g["text_id"], "i": g["original_item_id"]})
        await s.commit()

        res = await camp.fill_campaign_spins(run.id)
        assert res["ok"] and res["filled"] == 3 and res["groups"] == 1
        # ран обратно в READY (без старта постинга)
        st = await s.scalar(text("SELECT status FROM posting_runs WHERE id=:r"), {"r": run.id})
        assert st == "ready"
        # все айтемы постабельны + ссылка инжектнута
        items = (await s.execute(select(TextItem).where(
            TextItem.posting_run_id == run.id))).scalars().all()
        assert len(items) == 3 and all(it.text_id is not None for it in items)
        for it in items:
            body = await s.scalar(select(Text.body).where(Text.id == it.text_id))
            assert "https://x/" in body
        # идемпотентность: второй вызов — нечего расшивать, дублей нет
        res2 = await camp.fill_campaign_spins(run.id)
        assert res2["filled"] == 0
        cnt = await s.scalar(text(
            "SELECT count(*) FROM text_items WHERE posting_run_id=:r"), {"r": run.id})
        assert cnt == 3
    finally:
        await _cleanup(s, proj.id, extra_tids=[g["text_id"]])


async def test_fill_campaign_spins_skips_ungenerated(db_session, monkeypatch):
    """Оригинал ещё не сгенерён (пустое тело) → группа пропускается, спины не
    расшиваются (нельзя спинить пустоту)."""
    async def fake_pick(s, *, purpose, model_pk=None):
        return None

    monkeypatch.setattr(camp, "pick_model", fake_pick)
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign",
                                   content_mode="gen_per_row", run_mode="manual")
    await s.commit()
    rows = [{"link": "https://x/", "anchor": "a", "count": 2, "keyword": "k"}]
    total, groups, mains = await camp.create_empty_campaign_items(
        run.id, proj.id, rows, "gen_per_row", "English")
    run.gen_params = {"fanout_groups": groups, "main_text_ids": mains}
    await s.commit()
    g = groups[0]
    try:
        res = await camp.fill_campaign_spins(run.id)
        assert res["filled"] == 0 and res["ungenerated"] == 1
        empties = await s.scalar(text(
            "SELECT count(*) FROM text_items WHERE posting_run_id=:r AND text_id IS NULL"),
            {"r": run.id})
        assert empties == 2          # ничего не расшито
    finally:
        await _cleanup(s, proj.id, extra_tids=[g["text_id"]])


async def test_fill_spins_rejects_non_gen_per_row(client, auth_headers, db_session):
    s = db_session
    proj, run = await _mk_proj_run(s, status="ready", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post")
    await s.commit()
    try:
        r = await client.post(f"/admin/api/postings/{run.id}/fill-spins", headers=auth_headers)
        assert r.status_code == 409
    finally:
        await _cleanup(s, proj.id)


async def test_external_gen_pending_flag(db_session):
    """_external_gen_pending: пока gen_active=true И есть пустые айтемы — постинг
    ждёт (>0). Снят флаг ИЛИ нет пустых → 0 (постинг не виснет)."""
    from workers.celery.posting import _external_gen_pending
    s = db_session
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post")
    for i in range(3):
        s.add(TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                       content_hash=f"{run.id:062d}e{i}", byte_size=0, status="pending", text_id=None))
    await s.commit()
    try:
        # флаг не стоит → 0 (даже при пустых айтемах: их никто не наполнит)
        assert await _external_gen_pending(run.id) == 0
        # флаг стоит → ждём (3 пустых)
        await camp.set_gen_active(run.id, True)
        assert await _external_gen_pending(run.id) == 3
        # флаг снят → 0
        await camp.set_gen_active(run.id, False)
        assert await _external_gen_pending(run.id) == 0
    finally:
        await _cleanup(s, proj.id)


async def test_generate_run_items_keeps_running_status(db_session, monkeypatch):
    """Параллельный gen+post: если постинг уже подхватил ран (RUNNING), завершение
    manual-генерации НЕ перетирает статус обратно в READY (только из UNPACKING)."""
    async def fake_gen(model, prompt):
        return "<p>x with https://x/ link</p>"

    class _M:
        model_id = "m"

    async def fake_pick(s, *, purpose, model_pk=None):
        return _M()

    monkeypatch.setattr(camp, "_gen", fake_gen)
    monkeypatch.setattr(camp, "pick_model", fake_pick)
    s = db_session
    # ран уже в RUNNING (постинг подхватил), генерация наполняет тексты параллельно
    proj, run = await _mk_proj_run(s, status="running", task_type="post",
                                   content_source="csv_campaign", content_mode="gen_per_post",
                                   run_mode="manual", gen_params={"language": "English"})
    gr = {"keyword": "k", "link": "https://x/", "anchor": "a"}
    it = TextItem(posting_run_id=run.id, project_id=proj.id, original_filename="t",
                  content_hash=f"{run.id:062d}r1", byte_size=0, status="pending",
                  text_id=None, gen_row=gr)
    s.add(it)
    await s.commit()
    try:
        await camp.generate_run_items(run.id, finalize=False)
        st = await s.scalar(text("SELECT status FROM posting_runs WHERE id=:r"), {"r": run.id})
        assert st == "running"          # НЕ перетёрли в ready — постинг владеет статусом
        # gen_active снят (finally)
        ga = await s.scalar(text(
            "SELECT (gen_params->>'gen_active') FROM posting_runs WHERE id=:r"), {"r": run.id})
        assert ga in ("false", None)
    finally:
        await _cleanup(s, proj.id)


async def test_start_allows_unpacking_gen_per_post(client, auth_headers, db_session):
    """«Старт постинга» разрешён поверх идущей генерации (UNPACKING) для
    gen_per_post; для gen_per_row — нет (нужен fanout)."""
    s = db_session
    proj, run_pp = await _mk_proj_run(s, status="unpacking", task_type="post",
                                      content_source="csv_campaign", content_mode="gen_per_post")
    run_pr = PostingRun(project_id=proj.id, name="MGENPR", status="unpacking",
                        task_type="post", content_source="csv_campaign",
                        content_mode="gen_per_row", gen_params={"deferred_fanout": True})
    s.add(run_pr)
    await s.commit()
    try:
        r1 = await client.post(f"/admin/api/postings/{run_pp.id}/start", headers=auth_headers)
        assert r1.status_code == 202        # gen_per_post: разрешён поверх UNPACKING
        r2 = await client.post(f"/admin/api/postings/{run_pr.id}/start", headers=auth_headers)
        assert r2.status_code == 409        # gen_per_row: нельзя постить до fanout
    finally:
        await _cleanup(s, proj.id)
