"""Pydantic-схемы для /admin/api/dashboard."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from api.admin.schemas.postings import ProjectBrief, UserBrief


class DashboardCards(BaseModel):
    active_runs: int
    pending_texts: int
    posts_24h: int
    failed_24h: int
    wp_sites_active: int
    wp_credentials_valid: int


class DashboardRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    project: ProjectBrief
    creator: UserBrief | None
    total_texts: int
    posted_count: int
    failed_count: int
    skipped_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DashboardResponse(BaseModel):
    scope: str  # "all" | "limited"
    cards: DashboardCards
    active_runs: list[DashboardRun]
    recent_runs: list[DashboardRun]
