"""Pydantic-схемы для /admin/api/auth/*."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1)


class GroupBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class RoleBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    is_system: bool


class MeResponse(BaseModel):
    """То, что возвращает /admin/api/auth/me — UI на этом строит навигацию и кнопки."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    full_name: str | None
    is_active: bool
    is_super_admin: bool
    last_login_at: datetime | None
    created_at: datetime

    group: GroupBrief | None
    roles: list[RoleBrief]
    permissions: list[str]  # коды; для super_admin будет ["*"]
    accessible_pages: list[str]  # пути; для super_admin будет ["*"]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: MeResponse


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=200)


class ChangeEmailRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_email: EmailStr
