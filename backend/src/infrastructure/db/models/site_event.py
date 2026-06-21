"""SiteEvent — append-only лог ошибок по сайтам (миграция 0029).

Партиционирован по created_at помесячно. Пишут валидатор и постинг при
failure-ах. Успехи НЕ логируются (они в text_items / amount_use).

PK в БД — композитный (created_at, id) из-за партиционирования. ORM мапит id
как primary_key (id уникален через sequence) — для чтений/identity достаточно.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base


class SiteEvent(Base):
    __tablename__ = "site_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)       # validation | posting
    error_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posting_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
