"""
Celery application factory.

Используется для ТЯЖЁЛЫХ async-задач постинга (один прогон 1000+ текстов = одна
Celery task; внутри неё asyncio.gather + httpx.AsyncClient + семафор).

Лёгкие задачи (распаковка архива, валидация одной админки, scheduled crons) —
в TaskIQ (см. core.taskiq_app).
"""

from __future__ import annotations

from celery import Celery

from core.config import settings


def make_celery() -> Celery:
    app = Celery(
        "gym_app",
        broker=settings.CELERY_BROKER_URL,
        backend=settings.CELERY_RESULT_BACKEND,
        include=[
            "workers.celery.health",
            "workers.celery.posting",
        ],
    )

    app.conf.update(
        # Задачи и брокер общаются JSON-ом
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone=settings.TZ,
        enable_utc=False,
        # Не дублируем acks при ретраях; если воркер падает — задача не теряется
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Один воркер = один прогон постинга за раз (внутри сам параллелит через asyncio)
        worker_prefetch_multiplier=1,
        # Не убивать долгие таски через soft-timeout случайно
        task_soft_time_limit=None,
        task_time_limit=None,
        # Результат хранится 24 часа
        result_expires=24 * 60 * 60,
        # ── Приоритеты (Redis transport) ────────────────────────────
        # priority значение 0..9, где 0 = высший. См. PostingRunPriority.
        # Redis-брокер требует явно объявить шаги для создания подqueue-в.
        task_default_priority=5,
        broker_transport_options={
            "priority_steps": list(range(10)),
            "queue_order_strategy": "priority",
        },
    )

    return app


celery_app = make_celery()
