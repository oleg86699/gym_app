"""Простая ping-задача для проверки что воркер живой."""

from __future__ import annotations

from core.celery_app import celery_app


@celery_app.task(name="health.ping")
def ping() -> str:
    return "pong"
