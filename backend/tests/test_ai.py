"""AI: шаблонизатор промптов + выбор модели (C2-2)."""

from __future__ import annotations

from sqlalchemy import text as sql

from core.crypto import encrypt_password
from domain.ai import pick_model, render_prompt


def test_render_prompt_substitutes_known():
    body = "Write about {keyword} in {language}. Link {anchor} → {links}."
    out = render_prompt(body, {"keyword": "casino", "language": "en",
                               "anchor": "Nawal", "links": "https://nawal.mx/"})
    assert out == "Write about casino in en. Link Nawal → https://nawal.mx/."


def test_render_prompt_keeps_unknown():
    # неизвестный плейсхолдер не падает — остаётся как есть
    out = render_prompt("Hi {name}, {unknown}", {"name": "Bob"})
    assert out == "Hi Bob, {unknown}"


async def test_pick_model_by_purpose(db_session):
    s = db_session
    pid = (await s.execute(sql(
        "INSERT INTO ai_providers (name,type,api_key_enc,is_active,created_at) "
        "VALUES ('T','openai',:k,true,now()) RETURNING id"
    ), {"k": encrypt_password("sk-test")})).scalar_one()
    mid = (await s.execute(sql(
        "INSERT INTO ai_models (provider_id,display_name,model_id,temperature,max_tokens,"
        "purpose,is_active,created_at) VALUES (:p,'M','gpt-x',0.5,4096,'content',true,now()) "
        "RETURNING id"), {"p": pid})).scalar_one()
    await s.commit()
    try:
        # выбор по purpose возвращает активную content/any-модель (в общей dev-БД
        # их может быть несколько — не привязываемся к конкретному id)
        m = await pick_model(s, purpose="content")
        assert m is not None and m.purpose in ("content", "any")
        # точный выбор по pk
        exact = await pick_model(s, purpose="content", model_pk=mid)
        assert exact is not None and exact.id == mid
        # spin: своей spin-модели не создавали
        spin = await pick_model(s, purpose="spin")
        assert spin is None or spin.purpose in ("spin", "any")
    finally:
        await s.execute(sql("DELETE FROM ai_providers WHERE id=:p"), {"p": pid})
        await s.commit()
