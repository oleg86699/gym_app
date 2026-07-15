"""Видимость и права на AI-провайдеры (ключи) и промпты — зеркалит проекты.

Видимость (кто ВИДИТ ресурс): своё ∪ shared_all ∪ пошаренное лично ∪ пошаренное
его группе; group_admin вдобавок — всё, чем владеет его группа; super_admin — всё.

Управление (кто РЕДАКТИРУЕТ/шарит): владелец, super_admin, либо group_admin над
ресурсом своей группы.
"""
from __future__ import annotations

from sqlalchemy import or_, select

from infrastructure.db.models.ai import (
    AiProvider,
    PromptTemplate,
    ai_provider_groups,
    ai_provider_users,
    prompt_template_groups,
    prompt_template_users,
)


def visible_providers_filter(viewer):
    """SQLAlchemy-фильтр «какие провайдеры видит viewer», либо None (super → все)."""
    if viewer.is_super_admin:
        return None
    conds = [
        AiProvider.shared_all.is_(True),
        AiProvider.owner_user_id == viewer.id,
        AiProvider.id.in_(
            select(ai_provider_users.c.provider_id).where(
                ai_provider_users.c.admin_user_id == viewer.id)),
    ]
    if viewer.group_id is not None:
        conds.append(AiProvider.id.in_(
            select(ai_provider_groups.c.provider_id).where(
                ai_provider_groups.c.group_id == viewer.group_id)))
        if viewer.is_group_admin:
            conds.append(AiProvider.owner_group_id == viewer.group_id)
    return or_(*conds)


def visible_prompts_filter(viewer):
    """SQLAlchemy-фильтр «какие промпты видит viewer», либо None (super → все)."""
    if viewer.is_super_admin:
        return None
    conds = [
        PromptTemplate.shared_all.is_(True),
        PromptTemplate.owner_user_id == viewer.id,
        PromptTemplate.id.in_(
            select(prompt_template_users.c.prompt_id).where(
                prompt_template_users.c.admin_user_id == viewer.id)),
    ]
    if viewer.group_id is not None:
        conds.append(PromptTemplate.id.in_(
            select(prompt_template_groups.c.prompt_id).where(
                prompt_template_groups.c.group_id == viewer.group_id)))
        if viewer.is_group_admin:
            conds.append(PromptTemplate.owner_group_id == viewer.group_id)
    return or_(*conds)


def can_manage(viewer, resource) -> bool:
    """Редактировать/удалять/шарить: владелец, super_admin, либо group_admin над
    ресурсом своей группы. Работает и для провайдера, и для промпта, и для модели
    (у модели передаём её provider)."""
    if viewer.is_super_admin:
        return True
    if getattr(resource, "owner_user_id", None) == viewer.id:
        return True
    if (viewer.is_group_admin and viewer.group_id is not None
            and getattr(resource, "owner_group_id", None) == viewer.group_id):
        return True
    return False
