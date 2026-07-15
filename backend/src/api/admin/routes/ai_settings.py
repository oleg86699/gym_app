"""/admin/api/ai — провайдеры/модели/шаблоны промптов с владением и шарингом.

Читает — любой авторизованный (видит только доступное ему). Создаёт — любой
(своё, приватное). Редактирует/удаляет/шарит — владелец, super_admin, либо
group_admin над ресурсом своей группы. Ключи шифруются и наружу не отдаются.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user
from api.admin.schemas.ai_settings import (
    CreateModelRequest,
    CreatePromptRequest,
    CreateProviderRequest,
    ModelResponse,
    PromptResponse,
    ProviderResponse,
    ShareRequest,
    UpdateModelRequest,
    UpdatePromptRequest,
    UpdateProviderRequest,
)
from core.db import get_db_read, get_db_write
from domain.ai import (
    can_manage,
    create_model,
    create_prompt,
    create_provider,
    delete_model,
    delete_prompt,
    delete_provider,
    get_model,
    get_prompt,
    get_provider,
    list_prompts,
    list_providers,
    set_prompt_sharing,
    set_provider_sharing,
    update_model,
    update_prompt,
    update_provider,
)
from domain.audit.service import record as audit_record
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["ai_settings"])


# ─── Serialization ──────────────────────────────────────────────────
def _provider_out(p, viewer) -> ProviderResponse:
    return ProviderResponse(
        id=p.id, name=p.name, type=p.type, base_url=p.base_url, is_active=p.is_active,
        created_at=p.created_at, has_key=bool(p.api_key_enc),
        models=[ModelResponse.model_validate(m) for m in sorted(p.models, key=lambda m: m.id)],
        owner_user_id=p.owner_user_id,
        owner_username=(p.owner.username if p.owner else None),
        owner_group_id=p.owner_group_id,
        shared_all=p.shared_all,
        shared_user_ids=[u.id for u in p.shared_with_users],
        shared_group_ids=[g.id for g in p.shared_with_groups],
        can_manage=can_manage(viewer, p),
    )


def _prompt_out(t, viewer) -> PromptResponse:
    return PromptResponse(
        id=t.id, name=t.name, body=t.body, notes=t.notes, created_at=t.created_at,
        owner_user_id=t.owner_user_id,
        owner_username=(t.owner.username if t.owner else None),
        owner_group_id=t.owner_group_id,
        shared_all=t.shared_all,
        shared_user_ids=[u.id for u in t.shared_with_users],
        shared_group_ids=[g.id for g in t.shared_with_groups],
        can_manage=can_manage(viewer, t),
    )


async def _require_manage(viewer, resource, what: str):
    if resource is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    if not can_manage(viewer, resource):
        raise HTTPException(status_code=403, detail="Нет прав на этот ресурс")


async def _authorize_share(actor, *, is_prompt: bool, payload: ShareRequest,
                           session: AsyncSession) -> None:
    """Проверяет, что actor вправе так пошарить. Бросает 403 при нарушении."""
    # «Для всех» — только super_admin.
    if payload.shared_all and not actor.is_super_admin:
        raise HTTPException(status_code=403, detail="Открыть для всех может только super_admin")
    # Ключи (провайдеры) обычный пользователь не шарит вообще — они приватные.
    if not is_prompt and not (actor.is_super_admin or actor.is_group_admin):
        if payload.user_ids or payload.group_ids or payload.shared_all:
            raise HTTPException(status_code=403, detail="Свой ключ шарить нельзя — он приватный")
    if actor.is_super_admin:
        return
    # Не-super: группы — только своя.
    if payload.group_ids:
        if actor.group_id is None or any(g != actor.group_id for g in payload.group_ids):
            raise HTTPException(status_code=403, detail="Шарить можно только своей группе")
    # Не-super: точечно пользователям — только group_admin и только своей группе.
    if payload.user_ids:
        if not actor.is_group_admin or actor.group_id is None:
            raise HTTPException(status_code=403,
                                detail="Точечный шаринг пользователям — только для group_admin")
        targets = list((await session.scalars(
            select(AdminUser).where(AdminUser.id.in_(payload.user_ids)))).all())
        if len(targets) != len(set(payload.user_ids)) or any(
                u.group_id != actor.group_id for u in targets):
            raise HTTPException(status_code=403,
                                detail="Можно шарить только пользователям своей группы")


# ─── Providers ──────────────────────────────────────────────────────
@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[ProviderResponse]:
    return [_provider_out(p, viewer) for p in await list_providers(session, viewer)]


@router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider_endpoint(
    payload: CreateProviderRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProviderResponse:
    p = await create_provider(session, **payload.model_dump(),
                              owner_user_id=actor.id, owner_group_id=actor.group_id)
    log.info("ai.provider.created", actor_id=actor.id, provider_id=p.id)
    await audit_record(session, actor=actor, action="ai.provider.create",
                       resource_type="ai_provider", resource_id=p.id,
                       changes={"name": p.name, "type": p.type})
    return _provider_out(p, actor)


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider_endpoint(
    provider_id: int,
    payload: UpdateProviderRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProviderResponse:
    existing = await get_provider(session, provider_id)
    await _require_manage(actor, existing, "Provider")
    p = await update_provider(session, provider_id, **payload.model_dump(exclude_unset=True))
    await audit_record(session, actor=actor, action="ai.provider.update",
                       resource_type="ai_provider", resource_id=provider_id)
    return _provider_out(p, actor)


@router.post("/providers/{provider_id}/share", response_model=ProviderResponse)
async def share_provider_endpoint(
    provider_id: int,
    payload: ShareRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ProviderResponse:
    existing = await get_provider(session, provider_id)
    await _require_manage(actor, existing, "Provider")
    await _authorize_share(actor, is_prompt=False, payload=payload, session=session)
    p = await set_provider_sharing(session, provider_id, shared_all=payload.shared_all,
                                   user_ids=payload.user_ids, group_ids=payload.group_ids)
    await audit_record(session, actor=actor, action="ai.provider.share",
                       resource_type="ai_provider", resource_id=provider_id,
                       changes=payload.model_dump(exclude_none=True))
    return _provider_out(p, actor)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_endpoint(
    provider_id: int,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
):
    existing = await get_provider(session, provider_id)
    await _require_manage(actor, existing, "Provider")
    await delete_provider(session, provider_id)
    log.info("ai.provider.deleted", actor_id=actor.id, provider_id=provider_id)
    await audit_record(session, actor=actor, action="ai.provider.delete",
                       resource_type="ai_provider", resource_id=provider_id)


# ─── Models (наследуют владение своего провайдера) ──────────────────
@router.post("/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model_endpoint(
    payload: CreateModelRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ModelResponse:
    provider = await get_provider(session, payload.provider_id)
    await _require_manage(actor, provider, "Provider")
    m = await create_model(session, **payload.model_dump())
    await audit_record(session, actor=actor, action="ai.model.create",
                       resource_type="ai_model", resource_id=m.id,
                       changes={"model_id": m.model_id, "purpose": m.purpose})
    return ModelResponse.model_validate(m)


@router.patch("/models/{model_pk}", response_model=ModelResponse)
async def update_model_endpoint(
    model_pk: int,
    payload: UpdateModelRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> ModelResponse:
    existing = await get_model(session, model_pk)
    if existing is None:
        raise HTTPException(status_code=404, detail="Model not found")
    await _require_manage(actor, await get_provider(session, existing.provider_id), "Provider")
    m = await update_model(session, model_pk, **payload.model_dump(exclude_unset=True))
    await audit_record(session, actor=actor, action="ai.model.update",
                       resource_type="ai_model", resource_id=model_pk)
    return ModelResponse.model_validate(m)


@router.delete("/models/{model_pk}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_endpoint(
    model_pk: int,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
):
    existing = await get_model(session, model_pk)
    if existing is None:
        raise HTTPException(status_code=404, detail="Model not found")
    await _require_manage(actor, await get_provider(session, existing.provider_id), "Provider")
    await delete_model(session, model_pk)
    await audit_record(session, actor=actor, action="ai.model.delete",
                       resource_type="ai_model", resource_id=model_pk)


# ─── Prompt templates ───────────────────────────────────────────────
@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[PromptResponse]:
    return [_prompt_out(t, viewer) for t in await list_prompts(session, viewer)]


@router.post("/prompts", response_model=PromptResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt_endpoint(
    payload: CreatePromptRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> PromptResponse:
    t = await create_prompt(session, **payload.model_dump(),
                            owner_user_id=actor.id, owner_group_id=actor.group_id)
    await audit_record(session, actor=actor, action="ai.prompt.create",
                       resource_type="prompt_template", resource_id=t.id,
                       changes={"name": t.name})
    return _prompt_out(t, actor)


@router.patch("/prompts/{prompt_id}", response_model=PromptResponse)
async def update_prompt_endpoint(
    prompt_id: int,
    payload: UpdatePromptRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> PromptResponse:
    existing = await get_prompt(session, prompt_id)
    await _require_manage(actor, existing, "Prompt")
    t = await update_prompt(session, prompt_id, **payload.model_dump(exclude_unset=True))
    await audit_record(session, actor=actor, action="ai.prompt.update",
                       resource_type="prompt_template", resource_id=prompt_id)
    return _prompt_out(t, actor)


@router.post("/prompts/{prompt_id}/share", response_model=PromptResponse)
async def share_prompt_endpoint(
    prompt_id: int,
    payload: ShareRequest,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
) -> PromptResponse:
    existing = await get_prompt(session, prompt_id)
    await _require_manage(actor, existing, "Prompt")
    await _authorize_share(actor, is_prompt=True, payload=payload, session=session)
    t = await set_prompt_sharing(session, prompt_id, shared_all=payload.shared_all,
                                 user_ids=payload.user_ids, group_ids=payload.group_ids)
    await audit_record(session, actor=actor, action="ai.prompt.share",
                       resource_type="prompt_template", resource_id=prompt_id,
                       changes=payload.model_dump(exclude_none=True))
    return _prompt_out(t, actor)


@router.delete("/prompts/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_endpoint(
    prompt_id: int,
    actor: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_write),
):
    existing = await get_prompt(session, prompt_id)
    await _require_manage(actor, existing, "Prompt")
    await delete_prompt(session, prompt_id)
    await audit_record(session, actor=actor, action="ai.prompt.delete",
                       resource_type="prompt_template", resource_id=prompt_id)
