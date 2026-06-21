"""csv_campaign генерация: gen_per_post (мок LLM-вызова) (C2-3)."""

from __future__ import annotations

import domain.content_engine.campaign as camp
from core.crypto import encrypt_password
from domain.content_engine import (
    create_campaign_run,
    generate_campaign_run,
    start_campaign_fanout,
)
from infrastructure.db.models import TextItem
from sqlalchemy import select, text as sql


def test_parse_generated():
    t, b = camp._parse_generated("<title>Best Casinos 2026</title>\n<text>\n<p>Body…</p>\n</text>")
    assert t == "Best Casinos 2026" and b == "<p>Body…</p>"
    # без тегов — весь raw как тело
    assert camp._parse_generated("<p>plain</p>") == (None, "<p>plain</p>")
    # title без text → тело без title-тега
    assert camp._parse_generated("<title>X</title><p>body</p>") == ("X", "<p>body</p>")
    # markdown-fence снимаем
    t4, b4 = camp._parse_generated("```html\n<title>Y</title><text><p>z</p></text>\n```")
    assert t4 == "Y" and b4 == "<p>z</p>"


def test_row_language_overrides_form():
    # язык из строки файла важнее языка формы; форма — дефолт; иначе 'en'
    assert camp._row_vars({"language": "Spanish"}, "English")["language"] == "Spanish"
    assert camp._row_vars({"language": None}, "English")["language"] == "English"
    assert camp._row_vars({}, None)["language"] == "en"


async def test_gen_per_post_manual(db_session, monkeypatch):
    s = db_session
    pid = (await s.execute(sql(
        "INSERT INTO ai_providers (name,type,api_key_enc,is_active,created_at) "
        "VALUES ('CT','openai',:k,true,now()) RETURNING id"
    ), {"k": encrypt_password("sk-x")})).scalar_one()
    await s.execute(sql(
        "INSERT INTO ai_models (provider_id,display_name,model_id,temperature,max_tokens,"
        "purpose,is_active,created_at) VALUES (:p,'M','gpt-x',0.7,4096,'content',true,now())"
    ), {"p": pid})
    await s.commit()

    # мок LLM: возвращаем уникальные тела (по счётчику)
    calls = {"n": 0}

    async def fake_gen(session, *, model, prompt):
        calls["n"] += 1
        return f'<p>generated #{calls["n"]} <a href="https://nawal.mx/">Nawal</a></p>'
    monkeypatch.setattr(camp, "generate_text", fake_gen)

    run = await create_campaign_run(
        s, project_id=3, creator_id=None, name="CAMP POC",
        rows=[{"link": "https://nawal.mx/", "anchor": "Nawal", "count": 2, "keyword": "casino"}],
        content_mode="gen_per_post", run_mode="manual",
    )
    try:
        res = await generate_campaign_run(run.id)
        assert res["ok"] and res["items"] == 2 and calls["n"] == 2

        async with __import__("core.db", fromlist=["WriteSession"]).WriteSession() as s2:
            items = (await s2.execute(select(TextItem).where(
                TextItem.posting_run_id == run.id))).scalars().all()
            assert len(items) == 2
            assert all(it.status == "pending" and it.text_id and it.target_domain == "nawal.mx"
                       for it in items)
            status = await s2.scalar(sql("SELECT status FROM posting_runs WHERE id=:r"), {"r": run.id})
            assert status == "ready"  # manual → READY (ревью → Start)
            # прогресс генерации записан в gen_params (красный бар в UI/очереди)
            gp = await s2.scalar(sql("SELECT gen_params FROM posting_runs WHERE id=:r"), {"r": run.id})
            assert gp.get("gen_total") == 2 and gp.get("gen_done") == 2
            # тексты сгенерены, reusable
            cnt = await s2.scalar(sql(
                "SELECT count(*) FROM texts t JOIN text_items ti ON ti.text_id=t.id "
                "WHERE ti.posting_run_id=:r AND t.source='generated' AND t.reusable"), {"r": run.id})
            assert cnt == 2
    finally:
        async with __import__("core.db", fromlist=["WriteSession"]).WriteSession() as s2:
            ids = [r[0] for r in (await s2.execute(sql(
                "SELECT text_id FROM text_items WHERE posting_run_id=:r"), {"r": run.id})).all()]
            await s2.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
            if ids:
                await s2.execute(sql("DELETE FROM texts WHERE id = ANY(:i)"), {"i": ids})
            await s2.execute(sql("DELETE FROM ai_providers WHERE id=:p"), {"p": pid})
            await s2.commit()


async def test_reuse_mode_spins_and_bumps_counter(db_session):
    """reuse: reusable-оригинал со spin_formula → N уникальных вариантов
    (source='spin_variant', parent=оригинал) + times_used += N (без AI)."""
    s = db_session
    # уникальный lang изолирует reuse-пул только этим источником (в общей dev-БД
    # есть и другие reusable-тексты со spin_formula — иначе раскладка не детерм.)
    LANG = "qaiso"
    src_id = (await s.execute(sql(
        "INSERT INTO texts (body,title,lang,source,content_hash,times_used,spin_formula,"
        "reusable,used_as_original,created_at) VALUES "
        "(:b,'reuse src',:lang,'human',:h,0,:sf,true,false,now()) RETURNING id"
    ), {"b": "<p>play at {casino|gambling} site now</p>",
        "lang": LANG, "h": "reuse-src-hash-poc",
        "sf": "<p>play at {casino|gambling} site now</p>"})).scalar_one()
    await s.commit()

    run = await create_campaign_run(
        s, project_id=3, creator_id=None, name="REUSE POC",
        rows=[{"link": "https://nawal.mx/", "anchor": "Nawal", "count": 3}],
        content_mode="reuse", run_mode="manual", language=LANG,
    )
    try:
        res = await generate_campaign_run(run.id)
        assert res["ok"] and res["items"] == 3

        async with __import__("core.db", fromlist=["WriteSession"]).WriteSession() as s2:
            items = (await s2.execute(select(TextItem).where(
                TextItem.posting_run_id == run.id))).scalars().all()
            assert len(items) == 3
            assert all(it.status == "pending" and it.target_domain == "nawal.mx" for it in items)
            # варианты: spin_variant + parent=исходник, link инжектнут
            variants = await s2.scalar(sql(
                "SELECT count(*) FROM texts WHERE parent_text_id=:p AND source='spin_variant'"),
                {"p": src_id})
            assert variants == 3
            linked = await s2.scalar(sql(
                "SELECT count(*) FROM texts WHERE parent_text_id=:p AND body LIKE '%nawal.mx%'"),
                {"p": src_id})
            assert linked == 3
            # счётчик переиспользования исходника += 3
            tu = await s2.scalar(sql("SELECT times_used FROM texts WHERE id=:i"), {"i": src_id})
            assert tu == 3
            status = await s2.scalar(sql("SELECT status FROM posting_runs WHERE id=:r"), {"r": run.id})
            assert status == "ready"
    finally:
        async with __import__("core.db", fromlist=["WriteSession"]).WriteSession() as s2:
            ids = [r[0] for r in (await s2.execute(sql(
                "SELECT text_id FROM text_items WHERE posting_run_id=:r"), {"r": run.id})).all()]
            await s2.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
            if ids:
                await s2.execute(sql("DELETE FROM texts WHERE id = ANY(:i)"), {"i": ids})
            await s2.execute(sql("DELETE FROM texts WHERE id=:i"), {"i": src_id})
            await s2.commit()


async def test_gen_per_row_manual_defers_fanout(db_session, monkeypatch):
    """gen_per_row + manual: генерим ТОЛЬКО оригиналы (без text_items), READY →
    Start расшивает (отревьюенные) оригиналы в count вариантов."""
    s = db_session
    pid = (await s.execute(sql(
        "INSERT INTO ai_providers (name,type,api_key_enc,is_active,created_at) "
        "VALUES ('RT','openai',:k,true,now()) RETURNING id"
    ), {"k": encrypt_password("sk-x")})).scalar_one()
    await s.execute(sql(
        "INSERT INTO ai_models (provider_id,display_name,model_id,temperature,max_tokens,"
        "purpose,is_active,created_at) VALUES (:p,'M','gpt-x',0.7,4096,'content',true,now())"
    ), {"p": pid})
    # spin-модель: спинтакс теперь делается на Start (fanout), а не при генерации
    await s.execute(sql(
        "INSERT INTO ai_models (provider_id,display_name,model_id,temperature,max_tokens,"
        "purpose,is_active,created_at) VALUES (:p,'S','gpt-spin',0.7,4096,'spin',true,now())"
    ), {"p": pid})
    await s.commit()

    async def fake_gen(session, *, model, prompt):
        return "<p>{play|enjoy} the {casino|slots} now</p>"
    monkeypatch.setattr(camp, "generate_text", fake_gen)
    # не стучимся в Celery из теста
    import core.celery_app as cel
    monkeypatch.setattr(cel.celery_app, "send_task", lambda *a, **k: None)

    run = await create_campaign_run(
        s, project_id=3, creator_id=None, name="ROW POC",
        rows=[{"link": "https://nawal.mx/", "anchor": "Nawal", "count": 3, "keyword": "casino"}],
        content_mode="gen_per_row", run_mode="manual",
    )
    try:
        res = await generate_campaign_run(run.id)
        # манугал: item-ы созданы СРАЗУ (1 оригинал + 2 пустых), спин-заполнение на Start
        assert res["ok"] and res["items"] == 3 and res["originals"] == 1 and res["planned"] == 3

        WS = __import__("core.db", fromlist=["WriteSession"]).WriteSession
        async with WS() as s2:
            items = (await s2.execute(select(TextItem).where(
                TextItem.posting_run_id == run.id))).scalars().all()
            assert len(items) == 3                                  # N item-ов сразу
            with_text = [it for it in items if it.text_id is not None]
            assert len(with_text) == 1                              # 1 оригинал с текстом, 2 пустых
            assert all(it.status == "pending" and it.target_domain == "nawal.mx" for it in items)
            row = (await s2.execute(sql(
                "SELECT status, gen_params FROM posting_runs WHERE id=:r"), {"r": run.id})).first()
            assert row[0] == "ready"
            gp = row[1]
            assert gp.get("deferred_fanout") is True
            assert len(gp.get("main_text_ids") or []) == 1
            oid = gp["main_text_ids"][0]
            assert with_text[0].text_id == oid                     # текст оригинала привязан к item[0]
            # оригинал ПЛАЙН: спинтакс ещё НЕ сделан (делается на Start)
            assert await s2.scalar(sql("SELECT spin_formula FROM texts WHERE id=:i"), {"i": oid}) is None
            assert await s2.scalar(sql("SELECT body FROM texts WHERE id=:i"), {"i": oid}) \
                == "<p>{play|enjoy} the {casino|slots} now</p>"

        # Start → спинтакс + заполнение пустых item-ов
        sr = await start_campaign_fanout(run.id)
        assert sr["ok"] and sr["items"] == 3
        async with WS() as s2:
            items = (await s2.execute(select(TextItem).where(
                TextItem.posting_run_id == run.id))).scalars().all()
            assert len(items) == 3
            # все item-ы теперь заполнены (text_id проставлен) + pending + домен
            assert all(it.status == "pending" and it.text_id is not None
                       and it.target_domain == "nawal.mx" for it in items)
            variants = await s2.scalar(sql(
                "SELECT count(*) FROM texts WHERE parent_text_id=:p AND source='spin_variant' "
                "AND body LIKE '%nawal.mx%'"), {"p": oid})
            assert variants == 3
            # спинтакс сгенерён на Start (из отревьюенного тела) → spin_formula проставлен
            assert await s2.scalar(sql("SELECT spin_formula FROM texts WHERE id=:i"), {"i": oid}) is not None
            assert await s2.scalar(sql("SELECT times_used FROM texts WHERE id=:i"), {"i": oid}) == 3
            assert await s2.scalar(sql("SELECT status FROM posting_runs WHERE id=:r"), {"r": run.id}) == "queued"
            # идемпотентность (статус уже queued)
            assert (await start_campaign_fanout(run.id))["status"] == "already_started"
    finally:
        async with __import__("core.db", fromlist=["WriteSession"]).WriteSession() as s2:
            ids = [r[0] for r in (await s2.execute(sql(
                "SELECT text_id FROM text_items WHERE posting_run_id=:r"), {"r": run.id})).all()]
            gp = await s2.scalar(sql("SELECT gen_params FROM posting_runs WHERE id=:r"), {"r": run.id})
            ids += (gp or {}).get("main_text_ids") or []
            await s2.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
            if ids:
                await s2.execute(sql("DELETE FROM texts WHERE id = ANY(:i)"), {"i": list(set(ids))})
            await s2.execute(sql("DELETE FROM ai_providers WHERE id=:p"), {"p": pid})
            await s2.commit()
