"""Единая библиотека текстов (тела в БД). Все источники (ручные заливки,
позже — генератор/reuse) пишут сюда; постинг читает отсюда.

body_tsv — генерируемая STORED tsvector-колонка (управляется БД, в модели не
мапим). content_hash — под дедуп. lang/source/gen_model — метаданные.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR, Boolean, DateTime, ForeignKey, Integer, String, Text as SAText, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base


class Text(Base):
    __tablename__ = "texts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[str] = mapped_column(SAText, nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # human | generated | spin_variant | reused
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="human")
    gen_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, index=True)
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ── C1: спин + reuse + происхождение ──────────────────────────
    spin_formula: Mapped[str | None] = mapped_column(SAText, nullable=True)
    reusable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_as_original: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # производная (spin_variant) знает свой оригинал; NULL для оригиналов
    parent_text_id: Mapped[int | None] = mapped_column(
        ForeignKey("texts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # body_tsv — generated STORED, БД-managed, в модели не объявляем.
