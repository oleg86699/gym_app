"""
Project модель + ассоциативные таблицы для multi-tenant access.

Логика scope (см. domain/projects/service.py):
- super_admin → видит всё
- group_admin → видит проекты юзеров своей группы + projects, явно расшаренные
  его группе через `group_projects`
- manager → свои (owner_id) + projects расшаренные ему через `user_projects`
  + projects расшаренные его группе через `group_projects`

В этапе 1 модель минимальная — только идентификация и владение. Поля для
постинга (domain, статистика прогонов и т.д.) подъедут в этапе 2.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base, SoftDeletableMixin


# ─── Pivot tables ────────────────────────────────────────────────────

# Group ⇄ Project — группе явно открыт доступ к проекту, даже если проект
# создан не членом группы. Это "share с группой".
group_projects = Table(
    "group_projects",
    Base.metadata,
    Column(
        "group_id",
        Integer,
        ForeignKey("admin_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

# AdminUser ⇄ Project — индивидуальный shared доступ к проекту.
# Owner проекта НЕ требуется в этой таблице — он определяется через
# `projects.owner_user_id`.
user_projects = Table(
    "user_projects",
    Base.metadata,
    Column(
        "admin_user_id",
        Integer,
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# ─── Project ──────────────────────────────────────────────────────────


class Project(Base, SoftDeletableMixin):
    """
    Проект — сущность, к которой привязываются прогоны постинга и
    статистика. На этапе 1 только владелец и базовые поля. В этапе 2
    добавятся posting_runs, text_items, агрегированные счётчики.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Владелец — определяет основной scope доступа
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Денормализация: group_id владельца, кешируется при создании проекта
    # для быстрых group-scope запросов. Обновляется через миграции/триггер
    # если у юзера меняется группа (см. domain/users/service.py).
    owner_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # NB: лимит «сколько раз один WP-сайт можно использовать» переехал на ЗАДАЧУ
    # (posting_runs.max_posts_per_site, default 1) — см. миграцию 0040.

    # Relationships
    owner = relationship("AdminUser", foreign_keys=[owner_user_id])
    owner_group = relationship("AdminGroup", foreign_keys=[owner_group_id])

    shared_with_groups = relationship(
        "AdminGroup",
        secondary=group_projects,
        backref="shared_projects",
    )
    shared_with_users = relationship(
        "AdminUser",
        secondary=user_projects,
        backref="shared_projects",
    )


class ProjectDomain(Base):
    """Целевые (money) домены, которые продвигает проект.

    По ним из загружаемых текстов вытаскиваем «наши» бэклинки (совпадение
    домена href) и строим аналитику «сколько ссылок/текстов на домен».
    Это НЕ донор-сайты пула (те живут в wp_sites) — на них мы постим.
    """

    __tablename__ = "project_domains"
    __table_args__ = (UniqueConstraint("project_id", "domain", name="uq_project_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
