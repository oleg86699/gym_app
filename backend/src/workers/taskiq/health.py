"""Ping-task для TaskIQ — проверка живости воркера."""

from __future__ import annotations

from core.taskiq_app import broker


@broker.task(task_name="health.ping")
async def ping() -> str:
    return "pong"
