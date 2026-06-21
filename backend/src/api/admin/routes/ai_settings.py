"""/admin/api/ai — провайдеры/модели/шаблоны промптов (super_admin).

Ключи провайдеров шифруются и наружу не отдаются (только has_key).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user, require_super_admin
from api.admin.schemas.ai_settings import (
    CreateModelRequest,
    CreatePromptRequest,
    CreateProviderRequest,
    ModelResponse,
    PromptResponse,
    ProviderResponse,
    UpdateModelRequest,
    UpdatePromptRequest,
    UpdateProviderRequest,
)
from core.db import get_db_read, get_db_write
from domain.ai import (
    create_model,
    create_prompt,
    create_provider,
    delete_model,
    delete_prompt,
    delete_provider,
    get_provider,
    list_prompts,
    list_providers,
    update_model,
    update_prompt,
    update_provider,
)
from domain.audit.service import record as audit_record
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["ai_settings"])


def _provider_out(p) -> ProviderResponse:
    return ProviderResponse(
        id=p.id, name=p.name, type=p.type, base_url=p.base_url, is_active=p.is_active,
        created_at=p.created_at, has_key=bool(p.api_key_enc),
        models=[ModelResponse.model_validate(m) for m in sorted(p.models, key=lambda m: m.id)],
    )


# ─── Providers ──────────────────────────────────────────────────────
@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers_endpoint(
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[ProviderResponse]:
    return [_provider_out(p) for p in await list_providers(session)]


@router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider_endpoint(
    payload: CreateProviderRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ProviderResponse:
    p = await create_provider(session, **payload.model_dump())
    log.info("ai.provider.created", actor_id=actor.id, provider_id=p.id)
    await audit_record(session, actor=actor, action="ai.provider.create",
                       resource_type="ai_provider", resource_id=p.id,
                       changes={"name": p.name, "type": p.type})
    return _provider_out(p)


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider_endpoint(
    provider_id: int,
    payload: UpdateProviderRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ProviderResponse:
    p = await update_provider(session, provider_id, **payload.model_dump(exclude_unset=True))
    if p is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await audit_record(session, actor=actor, action="ai.provider.update",
                       resource_type="ai_provider", resource_id=provider_id)
    return _provider_out(p)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_endpoint(
    provider_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
):
    if await get_provider(session, provider_id) is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await delete_provider(session, provider_id)
    log.info("ai.provider.deleted", actor_id=actor.id, provider_id=provider_id)
    await audit_record(session, actor=actor, action="ai.provider.delete",
                       resource_type="ai_provider", resource_id=provider_id)


# ─── Models ─────────────────────────────────────────────────────────
@router.post("/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model_endpoint(
    payload: CreateModelRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ModelResponse:
    if await get_provider(session, payload.provider_id) is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    m = await create_model(session, **payload.model_dump())
    await audit_record(session, actor=actor, action="ai.model.create",
                       resource_type="ai_model", resource_id=m.id,
                       changes={"model_id": m.model_id, "purpose": m.purpose})
    return ModelResponse.model_validate(m)


@router.patch("/models/{model_pk}", response_model=ModelResponse)
async def update_model_endpoint(
    model_pk: int,
    payload: UpdateModelRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> ModelResponse:
    m = await update_model(session, model_pk, **payload.model_dump(exclude_unset=True))
    if m is None:
        raise HTTPException(status_code=404, detail="Model not found")
    await audit_record(session, actor=actor, action="ai.model.update",
                       resource_type="ai_model", resource_id=model_pk)
    return ModelResponse.model_validate(m)


@router.delete("/models/{model_pk}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_endpoint(
    model_pk: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
):
    await delete_model(session, model_pk)
    await audit_record(session, actor=actor, action="ai.model.delete",
                       resource_type="ai_model", resource_id=model_pk)


# ─── Prompt templates ───────────────────────────────────────────────
@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts_endpoint(
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> list[PromptResponse]:
    return [PromptResponse.model_validate(t) for t in await list_prompts(session)]


@router.post("/prompts", response_model=PromptResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt_endpoint(
    payload: CreatePromptRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> PromptResponse:
    t = await create_prompt(session, **payload.model_dump())
    await audit_record(session, actor=actor, action="ai.prompt.create",
                       resource_type="prompt_template", resource_id=t.id,
                       changes={"name": t.name})
    return PromptResponse.model_validate(t)


@router.patch("/prompts/{prompt_id}", response_model=PromptResponse)
async def update_prompt_endpoint(
    prompt_id: int,
    payload: UpdatePromptRequest,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
) -> PromptResponse:
    t = await update_prompt(session, prompt_id, **payload.model_dump(exclude_unset=True))
    if t is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    await audit_record(session, actor=actor, action="ai.prompt.update",
                       resource_type="prompt_template", resource_id=prompt_id)
    return PromptResponse.model_validate(t)


@router.delete("/prompts/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_endpoint(
    prompt_id: int,
    actor: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_write),
):
    await delete_prompt(session, prompt_id)
    await audit_record(session, actor=actor, action="ai.prompt.delete",
                       resource_type="prompt_template", resource_id=prompt_id)
