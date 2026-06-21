"""Pydantic-схемы для /admin/api/invitations и /public/invitations."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from api.admin.schemas.auth import GroupBrief


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str | None = None


class InvitationResponse(BaseModel):
    """Запись инвайта для списка/деталей. Сам токен не выдаётся."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    token_prefix: str
    created_by: UserBrief | None
    group: GroupBrief | None
    role_ids: list[int]
    email: str | None
    note: str | None
    expires_at: datetime
    is_revoked: bool
    used_at: datetime | None
    used_by: UserBrief | None
    created_at: datetime


class CreatedInvitationResponse(InvitationResponse):
    """Ответ на POST — содержит plain_token (один раз) и invite_url."""

    plain_token: str
    invite_url: str


class CreateInvitationRequest(BaseModel):
    group_id: int | None = None
    role_ids: list[int] = Field(default_factory=list)
    email: EmailStr | None = None
    note: str | None = Field(default=None, max_length=1000)
    ttl_hours: int = Field(default=12, ge=1, le=24 * 90)  # 12h default, max 90 дней


# ─── Public ──────────────────────────────────────────────────────────


class PublicInvitationView(BaseModel):
    """То, что видит приглашённый ДО регистрации. Минимум информации."""

    group_name: str | None
    invited_by_username: str | None
    email: str | None
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)
