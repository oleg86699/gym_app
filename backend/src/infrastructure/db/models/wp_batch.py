"""
WpImportBatch — группировка credentials по моменту/файлу импорта.

Жизненный цикл:
  uploaded → validating → done
              ↓     ↑
            paused (по запросу)

Credentials всё так же лежат в общем пуле (`wp_credentials`), батч —
только метаданные группы (исходный CSV в MinIO, кто загрузил, счётчики).
Удаление батча НЕ удаляет credentials — это soft-delete только записи
батча.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base, SoftDeletableMixin


class WpBatchStatus(StrEnum):
    UPLOADED = "uploaded"      # только что загружен, ещё не валидировали
    VALIDATING = "validating"  # сейчас идёт проверка
    PAUSED = "paused"          # пользователь поставил на паузу
    DONE = "done"              # проверка завершена (или была остановлена)


class WpImportBatch(Base, SoftDeletableMixin):
    __tablename__ = "wp_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Опц. данные о поставщике/стоимости для ROI-аналитики
    cost_total: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WpBatchStatus.UPLOADED.value
    )

    # Денормализованные счётчики (обновляются на import + validate)
    total_credentials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_credentials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # IDs «оригиналов» — cred в других batches, на которые попали duplicates
    # при импорте этого batch. Filter='duplicates' читает этот список.
    # Так избегаем засорения БД дублями, но даём возможность посмотреть.
    duplicate_cred_ids: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Управление воркером валидации
    pause_requested: Mapped[bool] = mapped_column(default=False, nullable=False)

    validation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_wp_batches_status_created", "status", "created_at"),
        Index("ix_wp_batches_deleted_at", "deleted_at"),
    )
