"""AI-инфра (C2): провайдеры, модели, шаблоны промптов.

Владение/шаринг (миграция 0059) зеркалит паттерн проектов:
- owner_user_id — владелец (кто создал), owner_group_id — денормализованный кэш
  группы владельца (для доступа group_admin);
- shared_all — виден ВСЕМ (ставит только super_admin, «дефолт для всех»);
- pivot-таблицы шаринга на конкретных пользователей и на группы.
Модели (AiModel) наследуют владение своего провайдера — своих прав не имеют.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base


# ─── Share pivots (кому открыт провайдер/промпт) ─────────────────────
ai_provider_users = Table(
    "ai_provider_users",
    Base.metadata,
    Column("provider_id", Integer, ForeignKey("ai_providers.id", ondelete="CASCADE"), primary_key=True),
    Column("admin_user_id", Integer, ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
)
ai_provider_groups = Table(
    "ai_provider_groups",
    Base.metadata,
    Column("provider_id", Integer, ForeignKey("ai_providers.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", Integer, ForeignKey("admin_groups.id", ondelete="CASCADE"), primary_key=True),
)
prompt_template_users = Table(
    "prompt_template_users",
    Base.metadata,
    Column("prompt_id", Integer, ForeignKey("prompt_templates.id", ondelete="CASCADE"), primary_key=True),
    Column("admin_user_id", Integer, ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
)
prompt_template_groups = Table(
    "prompt_template_groups",
    Base.metadata,
    Column("prompt_id", Integer, ForeignKey("prompt_templates.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", Integer, ForeignKey("admin_groups.id", ondelete="CASCADE"), primary_key=True),
)


class AiProvider(Base):
    __tablename__ = "ai_providers"
    # имя уникально В РАМКАХ ВЛАДЕЛЬЦА — у каждого свой namespace ключей.
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_ai_provider_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # openai|anthropic|google
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Владение / шаринг ──
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shared_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    models = relationship("AiModel", back_populates="provider", cascade="all, delete-orphan")
    owner = relationship("AdminUser", foreign_keys=[owner_user_id])
    owner_group = relationship("AdminGroup", foreign_keys=[owner_group_id])
    shared_with_users = relationship("AdminUser", secondary=ai_provider_users)
    shared_with_groups = relationship("AdminGroup", secondary=ai_provider_groups)


class AiModel(Base):
    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("ai_providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="content")  # content|spin|any
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    provider = relationship("AiProvider", back_populates="models")


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_prompt_template_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Владение / шаринг ──
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shared_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    owner = relationship("AdminUser", foreign_keys=[owner_user_id])
    owner_group = relationship("AdminGroup", foreign_keys=[owner_group_id])
    shared_with_users = relationship("AdminUser", secondary=prompt_template_users)
    shared_with_groups = relationship("AdminGroup", secondary=prompt_template_groups)
