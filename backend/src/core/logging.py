"""
Структурированное логирование через structlog.

JSON в проде, человекочитаемое в dev. trace_id middleware подъедет
в следующем чанке (chore/structured-logging-and-trace-id, расширим этот файл).
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    """Настроить stdlib logging + structlog."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib logging — базовая настройка
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Уменьшить шум от uvicorn access log в dev
    if not json:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # structlog — общие процессоры
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
