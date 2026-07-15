"""Схемы AI-настроек (C2-5): провайдеры, модели, шаблоны промптов.

Ключ провайдера принимается на вход, но НИКОГДА не возвращается — только
флаг has_key. Провайдеры/промпты владеемы и шарятся (owner + shared_all +
списки пользователей/групп).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_TYPE = "^(openai|anthropic|google)$"
_PURPOSE = "^(content|spin|any)$"


# ─── Provider ───────────────────────────────────────────────────────
class CreateProviderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern=_TYPE)
    api_key: str = Field(min_length=1, max_length=2000)
    base_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True


class UpdateProviderRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    type: str | None = Field(default=None, pattern=_TYPE)
    api_key: str | None = Field(default=None, max_length=2000)  # пусто → не менять
    base_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider_id: int
    display_name: str
    model_id: str
    temperature: float
    max_tokens: int
    purpose: str
    is_active: bool
    created_at: datetime


class ProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: str
    base_url: str | None
    is_active: bool
    created_at: datetime
    has_key: bool = True
    models: list[ModelResponse] = []
    # ── Владение / шаринг ──
    owner_user_id: int | None = None
    owner_username: str | None = None
    owner_group_id: int | None = None
    shared_all: bool = False
    shared_user_ids: list[int] = []
    shared_group_ids: list[int] = []
    can_manage: bool = False  # может ли текущий зритель редактировать/шарить


# ─── Model ──────────────────────────────────────────────────────────
class CreateModelRequest(BaseModel):
    provider_id: int
    display_name: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=120)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    purpose: str = Field(default="content", pattern=_PURPOSE)
    is_active: bool = True


class UpdateModelRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=120)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    purpose: str | None = Field(default=None, pattern=_PURPOSE)
    is_active: bool | None = None


# ─── Prompt template ────────────────────────────────────────────────
class CreatePromptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=100000)
    notes: str | None = Field(default=None, max_length=2000)


class UpdatePromptRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=100000)
    notes: str | None = Field(default=None, max_length=2000)


class PromptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    body: str
    notes: str | None
    created_at: datetime
    # ── Владение / шаринг ──
    owner_user_id: int | None = None
    owner_username: str | None = None
    owner_group_id: int | None = None
    shared_all: bool = False
    shared_user_ids: list[int] = []
    shared_group_ids: list[int] = []
    can_manage: bool = False


# ─── Sharing ────────────────────────────────────────────────────────
class ShareRequest(BaseModel):
    """Replace-семантика: переданные поля заменяют текущие наборы (None = не трогать)."""
    shared_all: bool | None = None
    user_ids: list[int] | None = None
    group_ids: list[int] | None = None
