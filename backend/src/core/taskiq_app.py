"""
TaskIQ broker и result backend.

Лёгкие async-задачи: распаковка zip с текстами, валидация одной WP-админки,
scheduled cron-ы (валидация всех админок раз в N часов, GC файлов в MinIO).
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, ListQueueBroker

from core.config import settings

broker = ListQueueBroker(
    url=settings.TASKIQ_BROKER_URL,
    queue_name="gym_app:taskiq",
).with_result_backend(RedisAsyncResultBackend(redis_url=settings.TASKIQ_RESULT_BACKEND))

# Scheduler для cron-задач — активируется в этапе 3 (валидация админок и т.п.)
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)
