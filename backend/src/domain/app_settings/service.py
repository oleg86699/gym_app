"""
Сервис для глобальных AppSettings (singleton row id=1).

Используется при создании PostingRun (concurrency/timeout/publish window)
и для UI super_admin.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.db.models import AppSettings

# Sentinel чтобы отличить "не передали поле" от "явно сбросили в NULL".
_UNSET: object = object()

# Жёсткие потолки чтобы super_admin случайно не положил систему.
MAX_CONCURRENCY = 100
MAX_TIMEOUT_SECONDS = 300
MIN_CONCURRENCY = 1
MIN_TIMEOUT_SECONDS = 5

# Глобальный потолок параллельных постов (across runs/процессы). Верх — щедрый,
# но защищает от «1000 одновременных» при множестве прогонов.
MAX_GLOBAL_POSTING_CONCURRENCY = 500
MIN_GLOBAL_POSTING_CONCURRENCY = 1

# CF Tier 3: одновременных браузер-контекстов (Patchright). Браузер тяжёлый —
# верхний потолок скромный, тюнится под RAM сервера.
MAX_CF_BROWSER_CONCURRENCY = 20
MIN_CF_BROWSER_CONCURRENCY = 1


async def get_app_settings(session: AsyncSession) -> AppSettings:
    """
    Получить настройки. Если строки нет (фоллбек, миграция могла не вставить) —
    создаём с дефолтами.
    """
    settings_row = await session.scalar(select(AppSettings).where(AppSettings.id == 1))
    if settings_row is None:
        settings_row = AppSettings(
            id=1,
            default_concurrency=25,
            default_timeout_seconds=30,
            global_posting_concurrency=80,
            default_publish_from=None,
            default_publish_to=None,
        )
        session.add(settings_row)
        await session.commit()
        settings_row = await session.scalar(select(AppSettings).where(AppSettings.id == 1))
        assert settings_row is not None
    return settings_row


async def update_app_settings(
    session: AsyncSession,
    *,
    default_concurrency: int | None = None,
    default_timeout_seconds: int | None = None,
    global_posting_concurrency: int | None = None,
    cf_browser_concurrency: int | None = None,
    default_publish_from: object = _UNSET,
    default_publish_to: object = _UNSET,
) -> AppSettings:
    row = await get_app_settings(session)
    if default_concurrency is not None:
        if not (MIN_CONCURRENCY <= default_concurrency <= MAX_CONCURRENCY):
            raise ValueError(
                f"default_concurrency must be in [{MIN_CONCURRENCY}, {MAX_CONCURRENCY}]"
            )
        row.default_concurrency = default_concurrency
    if global_posting_concurrency is not None:
        if not (MIN_GLOBAL_POSTING_CONCURRENCY <= global_posting_concurrency
                <= MAX_GLOBAL_POSTING_CONCURRENCY):
            raise ValueError(
                f"global_posting_concurrency must be in "
                f"[{MIN_GLOBAL_POSTING_CONCURRENCY}, {MAX_GLOBAL_POSTING_CONCURRENCY}]"
            )
        row.global_posting_concurrency = global_posting_concurrency
    if default_timeout_seconds is not None:
        if not (MIN_TIMEOUT_SECONDS <= default_timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ValueError(
                f"default_timeout_seconds must be in [{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}]"
            )
        row.default_timeout_seconds = default_timeout_seconds
    if cf_browser_concurrency is not None:
        if not (MIN_CF_BROWSER_CONCURRENCY <= cf_browser_concurrency
                <= MAX_CF_BROWSER_CONCURRENCY):
            raise ValueError(
                f"cf_browser_concurrency must be in "
                f"[{MIN_CF_BROWSER_CONCURRENCY}, {MAX_CF_BROWSER_CONCURRENCY}]"
            )
        row.cf_browser_concurrency = cf_browser_concurrency

    # Окно публикации: оба значения должны меняться согласованно (либо оба
    # заданы и from <= to, либо оба NULL).
    new_from: date | None = (
        default_publish_from if default_publish_from is not _UNSET else row.default_publish_from  # type: ignore[assignment]
    )
    new_to: date | None = (
        default_publish_to if default_publish_to is not _UNSET else row.default_publish_to  # type: ignore[assignment]
    )
    if (new_from is None) != (new_to is None):
        raise ValueError(
            "default_publish_from and default_publish_to must be both set or both empty"
        )
    if new_from is not None and new_to is not None and new_from > new_to:
        raise ValueError("default_publish_from must be <= default_publish_to")
    row.default_publish_from = new_from
    row.default_publish_to = new_to

    await session.commit()
    return row
