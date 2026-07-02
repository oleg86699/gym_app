"""Pydantic-схемы для /admin/api/projects/*."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api.admin.schemas.auth import GroupBrief


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str | None = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    deleted_at: datetime | None = None   # two-level delete: soft-deleted
    deleted_by: int | None = None        # admin_user.id, кто скрыл (super-аудит)
    deleted_by_user: UserBrief | None = None  # кто скрыл (для показа @username)

    owner: UserBrief
    owner_group: GroupBrief | None

    shared_with_users: list[UserBrief] = Field(default_factory=list)
    shared_with_groups: list[GroupBrief] = Field(default_factory=list)


class ProjectListItem(ProjectResponse):
    """ProjectResponse + live-метрики per project для overview-таблицы."""
    active_runs: int = 0          # queued/running/paused/scheduled/unpacking
    failed_runs: int = 0          # failed/need_more_admins/interrupted — требует внимания
    runs_total: int = 0           # сколько runs всего создано (lifetime)
    posted_total: int = 0         # text_items.status='posted' (lifetime)
    posted_24h: int = 0           # text_items posted за последние 24h
    last_activity_at: datetime | None = None  # max(last posted, last run created)
    # Постабельных САЙТОВ (= пул _pick_candidate_sites воркера: активный сайт +
    # ≥1 cred cred_status='valid' с каналом xmlrpc|admin), ещё не использованных
    # в проекте. Использован = ≥1 запись в project_wp_used (порог 1; лимит
    # повторов теперь per-задача, см. posting_runs.max_posts_per_site).
    available_admins: int = 0
    # Общий пул постабельных сайтов (контекст «X доступно из Y всего»).
    valid_admins_pool: int = 0


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    is_active: bool | None = None


class ShareUsersRequest(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


class ShareGroupsRequest(BaseModel):
    group_ids: list[int] = Field(default_factory=list)


class ReassignOwnerRequest(BaseModel):
    new_owner_id: int
