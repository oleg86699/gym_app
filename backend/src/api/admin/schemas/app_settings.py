"""Pydantic-схемы для /admin/api/app-settings."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from domain.app_settings.service import (
    MAX_CF_BROWSER_CONCURRENCY,
    MAX_CONCURRENCY,
    MAX_GLOBAL_POSTING_CONCURRENCY,
    MAX_TIMEOUT_SECONDS,
    MIN_CF_BROWSER_CONCURRENCY,
    MIN_CONCURRENCY,
    MIN_GLOBAL_POSTING_CONCURRENCY,
    MIN_TIMEOUT_SECONDS,
)


class AppSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    default_concurrency: int
    default_timeout_seconds: int
    global_posting_concurrency: int
    cf_browser_concurrency: int
    default_publish_from: date | None
    default_publish_to: date | None
    # Удобно для UI чтобы рендерить ограничения без хардкода
    limits: dict[str, int] = Field(
        default_factory=lambda: {
            "min_concurrency": MIN_CONCURRENCY,
            "max_concurrency": MAX_CONCURRENCY,
            "min_timeout_seconds": MIN_TIMEOUT_SECONDS,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
            "min_global_posting_concurrency": MIN_GLOBAL_POSTING_CONCURRENCY,
            "max_global_posting_concurrency": MAX_GLOBAL_POSTING_CONCURRENCY,
            "min_cf_browser_concurrency": MIN_CF_BROWSER_CONCURRENCY,
            "max_cf_browser_concurrency": MAX_CF_BROWSER_CONCURRENCY,
        }
    )


class UpdateAppSettingsRequest(BaseModel):
    """
    Опциональные поля. Окно публикации меняется парой: либо оба заданы, либо
    оба явные null (= сбросить).
    """

    model_config = ConfigDict(extra="forbid")

    default_concurrency: int | None = Field(
        default=None, ge=MIN_CONCURRENCY, le=MAX_CONCURRENCY
    )
    default_timeout_seconds: int | None = Field(
        default=None, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS
    )
    global_posting_concurrency: int | None = Field(
        default=None, ge=MIN_GLOBAL_POSTING_CONCURRENCY, le=MAX_GLOBAL_POSTING_CONCURRENCY
    )
    cf_browser_concurrency: int | None = Field(
        default=None, ge=MIN_CF_BROWSER_CONCURRENCY, le=MAX_CF_BROWSER_CONCURRENCY
    )
    # Внимание: чтобы отличить "не передали поле" от "передали null", используем
    # model_fields_set в роуте.
    default_publish_from: date | None = None
    default_publish_to: date | None = None
