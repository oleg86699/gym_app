"""
Базовый DeclarativeBase + mixin-ы для общих полей.

Все боевые модели наследуют один из mixin-ов:
- TimestampedMixin — для справочников и логов (created_at, updated_at).
- SoftDeletableMixin — для сущностей с soft-delete семантикой (+ deleted_at).

См. ADR-003.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Корневой DeclarativeBase для всех моделей."""


class TimestampedMixin:
    """`created_at` и `updated_at` — серверные дефолты, всегда заполнены."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeletableMixin(TimestampedMixin):
    """Добавляет `deleted_at`. Все default-ные запросы должны фильтровать `IS NULL`."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
