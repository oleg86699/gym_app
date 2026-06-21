"""AI-инфра CRUD (C2-5): провайдеры, модели, шаблоны промптов.

Ключи провайдеров шифруются Fernet (core.crypto) и наружу не отдаются —
в ответах только флаг has_key.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import encrypt_password
from infrastructure.db.models import AiModel, AiProvider, PromptTemplate

log = structlog.get_logger(__name__)


# ─── Providers ──────────────────────────────────────────────────────
async def list_providers(session: AsyncSession) -> list[AiProvider]:
    return list((await session.scalars(
        select(AiProvider).options(selectinload(AiProvider.models))
        .order_by(AiProvider.id))).all())


async def get_provider(session: AsyncSession, provider_id: int) -> AiProvider | None:
    return await session.scalar(
        select(AiProvider).options(selectinload(AiProvider.models))
        .where(AiProvider.id == provider_id))


async def create_provider(session: AsyncSession, *, name: str, type: str,
                           api_key: str, base_url: str | None = None,
                           is_active: bool = True) -> AiProvider:
    p = AiProvider(name=name.strip(), type=type, api_key_enc=encrypt_password(api_key),
                   base_url=(base_url or None), is_active=is_active)
    session.add(p)
    await session.commit()
    return await get_provider(session, p.id)  # type: ignore[return-value]


async def update_provider(session: AsyncSession, provider_id: int, *,
                          name: str | None = None, type: str | None = None,
                          api_key: str | None = None, base_url: str | None = None,
                          is_active: bool | None = None) -> AiProvider | None:
    vals: dict = {}
    if name is not None:
        vals["name"] = name.strip()
    if type is not None:
        vals["type"] = type
    if api_key:  # пустую строку игнорируем — ключ не меняем
        vals["api_key_enc"] = encrypt_password(api_key)
    if base_url is not None:
        vals["base_url"] = base_url or None
    if is_active is not None:
        vals["is_active"] = is_active
    if vals:
        await session.execute(update(AiProvider).where(AiProvider.id == provider_id).values(**vals))
        await session.commit()
    return await get_provider(session, provider_id)


async def delete_provider(session: AsyncSession, provider_id: int) -> None:
    await session.execute(delete(AiProvider).where(AiProvider.id == provider_id))
    await session.commit()


# ─── Models ─────────────────────────────────────────────────────────
async def create_model(session: AsyncSession, *, provider_id: int, display_name: str,
                       model_id: str, temperature: float = 0.7, max_tokens: int = 4096,
                       purpose: str = "content", is_active: bool = True) -> AiModel:
    m = AiModel(provider_id=provider_id, display_name=display_name.strip(),
                model_id=model_id.strip(), temperature=temperature, max_tokens=max_tokens,
                purpose=purpose, is_active=is_active)
    session.add(m)
    await session.commit()
    return await session.scalar(select(AiModel).where(AiModel.id == m.id))  # type: ignore[return-value]


async def update_model(session: AsyncSession, model_pk: int, **fields) -> AiModel | None:
    vals = {k: v for k, v in fields.items() if v is not None}
    if vals:
        await session.execute(update(AiModel).where(AiModel.id == model_pk).values(**vals))
        await session.commit()
    return await session.scalar(select(AiModel).where(AiModel.id == model_pk))


async def delete_model(session: AsyncSession, model_pk: int) -> None:
    await session.execute(delete(AiModel).where(AiModel.id == model_pk))
    await session.commit()


# ─── Prompt templates ───────────────────────────────────────────────
async def list_prompts(session: AsyncSession) -> list[PromptTemplate]:
    return list((await session.scalars(
        select(PromptTemplate).order_by(PromptTemplate.id))).all())


async def create_prompt(session: AsyncSession, *, name: str, body: str,
                        notes: str | None = None) -> PromptTemplate:
    t = PromptTemplate(name=name.strip(), body=body, notes=(notes or None))
    session.add(t)
    await session.commit()
    return await session.scalar(select(PromptTemplate).where(PromptTemplate.id == t.id))  # type: ignore[return-value]


async def update_prompt(session: AsyncSession, prompt_id: int, *, name: str | None = None,
                        body: str | None = None, notes: str | None = None) -> PromptTemplate | None:
    vals: dict = {}
    if name is not None:
        vals["name"] = name.strip()
    if body is not None:
        vals["body"] = body
    if notes is not None:
        vals["notes"] = notes or None
    if vals:
        await session.execute(update(PromptTemplate).where(PromptTemplate.id == prompt_id).values(**vals))
        await session.commit()
    return await session.scalar(select(PromptTemplate).where(PromptTemplate.id == prompt_id))


async def delete_prompt(session: AsyncSession, prompt_id: int) -> None:
    await session.execute(delete(PromptTemplate).where(PromptTemplate.id == prompt_id))
    await session.commit()
