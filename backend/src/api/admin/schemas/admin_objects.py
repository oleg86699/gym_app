"""Схемы для groups, roles, permissions, pages."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ─── Groups ───────────────────────────────────────────────────────────


class GroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    # tag-access RBAC: потолок разрешённых команде батч-тегов. null = все теги.
    allowed_tags: list[str] | None = None


class ProjectChip(BaseModel):
    """Краткая инфа о проекте — для chips в группе/юзере."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class GroupListResponse(GroupResponse):
    """С агрегатами для группового списка: количество юзеров, проекты-чипы."""
    members_count: int
    owned_projects: list[ProjectChip] = Field(default_factory=list)
    shared_projects: list[ProjectChip] = Field(default_factory=list)


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    is_active: bool = True


class UpdateGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    is_active: bool | None = None
    # Полная замена списка проектов, явно расшаренных группе (group_projects).
    shared_project_ids: list[int] | None = None
    # tag-access RBAC: потолок разрешённых команде батч-тегов (super_admin only).
    # null → снять ограничение; [..] → задать. «не трогать» vs «задать» — по
    # model_fields_set.
    allowed_tags: list[str] | None = None


# ─── Roles ────────────────────────────────────────────────────────────


class PermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    resource: str
    action: str
    description: str | None


class PageBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    path: str
    name: str
    is_active: bool


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    is_active: bool
    is_system: bool
    is_assignable_by_group_admin: bool
    permissions: list[PermissionResponse]
    pages: list[PageBrief]


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    permission_ids: list[int] = Field(default_factory=list)
    page_ids: list[int] = Field(default_factory=list)
    is_assignable_by_group_admin: bool = False


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    is_active: bool | None = None
    is_assignable_by_group_admin: bool | None = None
    permission_ids: list[int] | None = None
    page_ids: list[int] | None = None


# ─── Pages ────────────────────────────────────────────────────────────


class PageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    path: str
    name: str
    description: str | None
    is_active: bool
    created_at: datetime


class PageWithAssignments(PageResponse):
    role_ids: list[int]
    user_ids: list[int]


class UpdatePageRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    role_ids: list[int] | None = None
    user_ids: list[int] | None = None
