"""
Audit log — append-only лог ключевых действий.

Записывается из API mutation-эндпоинтов после успешного коммита. Не покрывает
read-only обращения (зашумит). Чем что:

- action: short slug, "users.create" / "runs.delete" / "proxies.import"
- resource_type: "user" / "run" / "project" / "proxy" / ...
- resource_id: nullable (для bulk-операций или actions без single resource)
- changes: JSONB с before/after или params (зависит от действия)
- actor_user_id: кто совершил. NULL только для system-actions.

Поиск типичный — по actor + action + date range, что хорошо покрывается
двумя индексами ниже.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_log_created", "created_at"),
        Index("ix_audit_log_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_log_action_created", "action", "created_at"),
    )
