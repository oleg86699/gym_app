"""Pydantic-схемы для /admin/api/users/*."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from api.admin.schemas.auth import GroupBrief, RoleBrief


class ProjectBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    is_active: bool


class UserResponse(BaseModel):
    """Краткая схема — для списков."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    full_name: str | None
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime

    group: GroupBrief | None
    roles: list[RoleBrief]


class UserDetailResponse(UserResponse):
    """Расширенная схема — для GET /users/{id} и edit-страницы."""

    # Проекты, к которым юзер имеет ИНДИВИДУАЛЬНЫЙ shared access (через user_projects).
    # Не включает свои собственные и не включает доступ через группу.
    shared_projects: list[ProjectBrief] = Field(default_factory=list)

    # Страницы, выданные юзеру индивидуально (через admin_user_pages).
    # Не включает страницы, доступные через роли.
    direct_page_ids: list[int] = Field(default_factory=list)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)
    group_id: int | None = None
    role_ids: list[int] = Field(default_factory=list)
    is_active: bool = True


class UpdateUserRequest(BaseModel):
    # Все поля опциональны — отправляются только изменённые
    username: str | None = Field(default=None, min_length=3, max_length=100)
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    is_active: bool | None = None
    # Опциональная смена пароля (если non-empty — заменит)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    # group_id: int → присвоить, None → не трогать.
    # Чтобы убрать юзера из группы — is_remove_from_group=true.
    group_id: int | None = None
    is_remove_from_group: bool = False
    role_ids: list[int] | None = None
    # Полный replace списка индивидуальных project-share-ов.
    project_ids: list[int] | None = None
    # Полный replace списка индивидуально выданных страниц.
    page_ids: list[int] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=200)
