"""AI-инфра CRUD (C2-5): провайдеры/модели/промпты + шифрование ключа."""

from __future__ import annotations

from core.crypto import decrypt_password
from domain.ai import (
    create_model,
    create_prompt,
    create_provider,
    delete_provider,
    list_prompts,
    list_providers,
    update_model,
    update_provider,
)
from sqlalchemy import text as sql


async def test_provider_model_prompt_lifecycle(db_session):
    s = db_session
    p = await create_provider(s, name="CRUD-OAI", type="openai", api_key="sk-secret-xyz")
    pid = p.id
    try:
        # ключ зашифрован и расшифровывается обратно
        enc = await s.scalar(sql("SELECT api_key_enc FROM ai_providers WHERE id=:i"), {"i": pid})
        assert enc != "sk-secret-xyz" and decrypt_password(enc) == "sk-secret-xyz"

        m = await create_model(s, provider_id=pid, display_name="GPT", model_id="gpt-4o-mini",
                               purpose="content")
        # вложенная модель видна в list_providers (в проде — свежая сессия на запрос)
        s.expire_all()
        provs = await list_providers(s)
        mine = next(x for x in provs if x.id == pid)
        assert any(mm.id == m.id and mm.model_id == "gpt-4o-mini" for mm in mine.models)

        # update model + provider
        await update_model(s, m.id, temperature=0.2, is_active=False)
        m2 = await s.scalar(sql("SELECT temperature, is_active FROM ai_models WHERE id=:i"), {"i": m.id})
        assert m2 is not None
        await update_provider(s, pid, name="CRUD-OAI-2")
        # пустой api_key → ключ не меняется
        await update_provider(s, pid, api_key="")
        assert decrypt_password(await s.scalar(
            sql("SELECT api_key_enc FROM ai_providers WHERE id=:i"), {"i": pid})) == "sk-secret-xyz"

        # prompt CRUD
        t = await create_prompt(s, name="CRUD-PROMPT", body="Write about {keyword}")
        assert any(x.id == t.id for x in await list_prompts(s))
        await s.execute(sql("DELETE FROM prompt_templates WHERE id=:i"), {"i": t.id})
        await s.commit()

        # delete provider каскадит модели
        await delete_provider(s, pid)
        assert await s.scalar(sql("SELECT count(*) FROM ai_models WHERE provider_id=:i"), {"i": pid}) == 0
        pid = None
    finally:
        if pid is not None:
            await s.execute(sql("DELETE FROM ai_providers WHERE id=:i"), {"i": pid})
            await s.commit()
