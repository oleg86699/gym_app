"""Схемы для доменов проекта + резолва needs_review-задач (Фаза A)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectDomainResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: str
    created_at: datetime


class AddProjectDomainRequest(BaseModel):
    # принимаем URL или голый домен — нормализуем на бэке
    domain: str = Field(min_length=3, max_length=255)


class AddProjectDomainResult(BaseModel):
    domain: str
    created: bool
    auto_resolved_runs: int


class BulkAddDomainsRequest(BaseModel):
    """Список доменов разом (по одному на строку / через запятую — парсим)."""
    domains: list[str] = Field(min_length=1, max_length=1000)


class BulkAddDomainsResult(BaseModel):
    added: list[str]
    duplicates: list[str]
    invalid: list[str]
    auto_resolved_runs: int


class ResolveTextItemRequest(BaseModel):
    """Дозаполнить needs_review-задачу: целевая ссылка + анкор."""
    link: str = Field(min_length=4, max_length=2000)
    anchor: str = Field(default="", max_length=500)


class DomainAnalyticsRow(BaseModel):
    target_domain: str
    total: int
    posted: int


class DomainSummaryResponse(BaseModel):
    domain: str
    total: int
    posted: int
    failed: int
    skipped: int
    in_progress: int
    sites: int
    runs: int
    last_posted_at: datetime | None = None


class DomainItemRow(BaseModel):
    id: int
    status: str
    link_url: str | None = None
    link_anchor: str | None = None
    posted_url: str | None = None
    posted_at: datetime | None = None
    last_error: str | None = None
    run_id: int
    run_name: str | None = None
    site_domain: str | None = None


class DomainItemsResponse(BaseModel):
    items: list[DomainItemRow]
    next_cursor: int | None = None
    has_more: bool = False


class DomainRunRow(BaseModel):
    id: int
    name: str
    status: str
    task_type: str = "post"
    content_source: str | None = None
    content_mode: str | None = None
    run_mode: str | None = None
    scheduled_for: datetime | None = None
    created_at: datetime
    total: int
    posted: int
    failed: int
