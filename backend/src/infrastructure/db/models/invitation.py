"""
Invitation tokens — приглашения на регистрацию.

Безопасность:
- В БД храним только sha256 hash токена, не сам токен.
- Полный токен показывается приглашающему один раз при создании.
- Токен 256-битный (secrets.token_urlsafe(32)).
- Срок жизни ограничен (expires_at), one-shot по умолчанию.
- Scope зашит при создании: group_id и role_ids нельзя поменять при использовании.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base, TimestampedMixin


class Invitation(Base, TimestampedMixin):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # sha256(token) в hex. Уникальный индекс для быстрой проверки.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Префикс токена (первые 8 символов) для отображения в списке как "abcd1234..."
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)

    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # В какую группу автоматически попадёт юзер (nullable — без группы).
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Какие роли получит. Хранятся как массив id; если пусто — назначится дефолтная "user".
    role_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, nullable=False)

    # Опциональный email для записи (можно prefill в форме регистрации).
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Опциональная заметка, для чего ссылка ("приглашение для Bob")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Когда и кем использован
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    created_by = relationship("AdminUser", foreign_keys=[created_by_user_id])
    group = relationship("AdminGroup", foreign_keys=[group_id])
    used_by = relationship("AdminUser", foreign_keys=[used_by_user_id])
