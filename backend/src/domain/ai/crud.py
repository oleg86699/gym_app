"""AI-инфра CRUD (C2-5): провайдеры, модели, шаблоны промптов.

Ключи провайдеров шифруются Fernet (core.crypto) и наружу не отдаются —
в ответах только флаг has_key. Провайдеры/промпты владеемы и шарятся
(owner_user_id/owner_group_id/shared_all + pivot-таблицы) — см. domain/ai/access.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import encrypt_password
from domain.ai.access import visible_prompts_filter, visible_providers_filter
from infrastructure.db.models import AdminGroup, AdminUser, AiModel, AiProvider, PromptTemplate

log = structlog.get_logger(__name__)

_PROVIDER_LOADS = (
    selectinload(AiProvider.models),
    selectinload(AiProvider.owner),
    selectinload(AiProvider.shared_with_users),
    selectinload(AiProvider.shared_with_groups),
)
_PROMPT_LOADS = (
    selectinload(PromptTemplate.owner),
    selectinload(PromptTemplate.shared_with_users),
    selectinload(PromptTemplate.shared_with_groups),
)


# ─── Providers ──────────────────────────────────────────────────────
async def list_providers(session: AsyncSession, viewer=None) -> list[AiProvider]:
    stmt = select(AiProvider).options(*_PROVIDER_LOADS).order_by(AiProvider.id)
    if viewer is not None:
        flt = visible_providers_filter(viewer)
        if flt is not None:
            stmt = stmt.where(flt)
    return list((await session.scalars(stmt)).all())


async def get_provider(session: AsyncSession, provider_id: int) -> AiProvider | None:
    return await session.scalar(
        select(AiProvider).options(*_PROVIDER_LOADS).where(AiProvider.id == provider_id))


async def create_provider(session: AsyncSession, *, name: str, type: str,
                          api_key: str, base_url: str | None = None,
                          is_active: bool = True, owner_user_id: int | None = None,
                          owner_group_id: int | None = None) -> AiProvider:
    p = AiProvider(name=name.strip(), type=type, api_key_enc=encrypt_password(api_key),
                   base_url=(base_url or None), is_active=is_active,
                   owner_user_id=owner_user_id, owner_group_id=owner_group_id)
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


async def get_model(session: AsyncSession, model_pk: int) -> AiModel | None:
    return await session.scalar(select(AiModel).where(AiModel.id == model_pk))


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
async def list_prompts(session: AsyncSession, viewer=None) -> list[PromptTemplate]:
    stmt = select(PromptTemplate).options(*_PROMPT_LOADS).order_by(PromptTemplate.id)
    if viewer is not None:
        flt = visible_prompts_filter(viewer)
        if flt is not None:
            stmt = stmt.where(flt)
    return list((await session.scalars(stmt)).all())


async def get_prompt(session: AsyncSession, prompt_id: int) -> PromptTemplate | None:
    return await session.scalar(
        select(PromptTemplate).options(*_PROMPT_LOADS).where(PromptTemplate.id == prompt_id))


async def create_prompt(session: AsyncSession, *, name: str, body: str,
                        notes: str | None = None, owner_user_id: int | None = None,
                        owner_group_id: int | None = None) -> PromptTemplate:
    t = PromptTemplate(name=name.strip(), body=body, notes=(notes or None),
                       owner_user_id=owner_user_id, owner_group_id=owner_group_id)
    session.add(t)
    await session.commit()
    return await get_prompt(session, t.id)  # type: ignore[return-value]


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
    return await get_prompt(session, prompt_id)


async def delete_prompt(session: AsyncSession, prompt_id: int) -> None:
    await session.execute(delete(PromptTemplate).where(PromptTemplate.id == prompt_id))
    await session.commit()


# ─── Sharing (replace-semantics) ────────────────────────────────────
async def _apply_sharing(session, obj, *, shared_all, user_ids, group_ids) -> None:
    """Заменяет наборы шаринга объекта на переданные (None = не трогать поле)."""
    if shared_all is not None:
        obj.shared_all = bool(shared_all)
    if user_ids is not None:
        users = list((await session.scalars(
            select(AdminUser).where(AdminUser.id.in_(user_ids or [-1])))).all())
        obj.shared_with_users = users
    if group_ids is not None:
        groups = list((await session.scalars(
            select(AdminGroup).where(AdminGroup.id.in_(group_ids or [-1])))).all())
        obj.shared_with_groups = groups
    await session.commit()


async def set_provider_sharing(session, provider_id, *, shared_all=None,
                               user_ids=None, group_ids=None) -> AiProvider | None:
    p = await get_provider(session, provider_id)
    if p is None:
        return None
    await _apply_sharing(session, p, shared_all=shared_all, user_ids=user_ids, group_ids=group_ids)
    return await get_provider(session, provider_id)


async def set_prompt_sharing(session, prompt_id, *, shared_all=None,
                             user_ids=None, group_ids=None) -> PromptTemplate | None:
    t = await get_prompt(session, prompt_id)
    if t is None:
        return None
    await _apply_sharing(session, t, shared_all=shared_all, user_ids=user_ids, group_ids=group_ids)
    return await get_prompt(session, prompt_id)
