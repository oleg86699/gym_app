"""
Сервис для глобальных AppSettings (singleton row id=1).

Используется при создании PostingRun (concurrency/timeout/publish window)
и для UI super_admin.
"""

from __future__ import annotations

from datetime import date, timedelta

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

# Fair-share floor — минимум конкурентности прогона при множестве активных.
MIN_CONCURRENCY_FLOOR = 1
MAX_CONCURRENCY_FLOOR = 100

# Сколько батчей валидируется одновременно (остальные в очереди).
MIN_MAX_CONCURRENT_BATCH_VALIDATIONS = 1
MAX_MAX_CONCURRENT_BATCH_VALIDATIONS = 20

# Пороги авто-выключения сайта (site-class фейлы подряд). Общий и отдельный CF.
MIN_SITE_DISABLE_THRESHOLD = 3
MAX_SITE_DISABLE_THRESHOLD = 200
MIN_SITE_DISABLE_THRESHOLD_CF = 1
MAX_SITE_DISABLE_THRESHOLD_CF = 100

# Дефолтное окно публикации для свежей установки: [сегодня−N .. сегодня].
# В ПРОШЛОМ (не в будущем!) — посты получают прошедшую дату и сразу видны,
# а не уходят в Scheduled. Пользователь меняет под себя в настройках.
DEFAULT_PUBLISH_WINDOW_DAYS = 45


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
            default_publish_from=date.today() - timedelta(days=DEFAULT_PUBLISH_WINDOW_DAYS),
            default_publish_to=date.today(),
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
    posting_concurrency_floor: int | None = None,
    site_disable_threshold: int | None = None,
    site_disable_threshold_cf: int | None = None,
    max_concurrent_batch_validations: int | None = None,
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
    if posting_concurrency_floor is not None:
        if not (MIN_CONCURRENCY_FLOOR <= posting_concurrency_floor <= MAX_CONCURRENCY_FLOOR):
            raise ValueError(
                f"posting_concurrency_floor must be in "
                f"[{MIN_CONCURRENCY_FLOOR}, {MAX_CONCURRENCY_FLOOR}]"
            )
        row.posting_concurrency_floor = posting_concurrency_floor
    if max_concurrent_batch_validations is not None:
        if not (MIN_MAX_CONCURRENT_BATCH_VALIDATIONS <= max_concurrent_batch_validations
                <= MAX_MAX_CONCURRENT_BATCH_VALIDATIONS):
            raise ValueError(
                f"max_concurrent_batch_validations must be in "
                f"[{MIN_MAX_CONCURRENT_BATCH_VALIDATIONS}, {MAX_MAX_CONCURRENT_BATCH_VALIDATIONS}]"
            )
        row.max_concurrent_batch_validations = max_concurrent_batch_validations
    if site_disable_threshold is not None:
        if not (MIN_SITE_DISABLE_THRESHOLD <= site_disable_threshold <= MAX_SITE_DISABLE_THRESHOLD):
            raise ValueError(
                f"site_disable_threshold must be in "
                f"[{MIN_SITE_DISABLE_THRESHOLD}, {MAX_SITE_DISABLE_THRESHOLD}]"
            )
        row.site_disable_threshold = site_disable_threshold
    if site_disable_threshold_cf is not None:
        if not (MIN_SITE_DISABLE_THRESHOLD_CF <= site_disable_threshold_cf
                <= MAX_SITE_DISABLE_THRESHOLD_CF):
            raise ValueError(
                f"site_disable_threshold_cf must be in "
                f"[{MIN_SITE_DISABLE_THRESHOLD_CF}, {MAX_SITE_DISABLE_THRESHOLD_CF}]"
            )
        row.site_disable_threshold_cf = site_disable_threshold_cf

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
