"""
RBAC модели: AdminUser × AdminGroup × AdminRole × AdminPermission × AdminPage.

Структура — из ADR-005. Ключевые правила:
- super_admin / group_admin / manager — это **роли** (записи в admin_roles),
  не флаги. Системные роли защищены `is_system=true`.
- Видимость данных по scope (super_admin → всё, group_admin → группа,
  manager → своё) реализуется на уровне data-access слоя, не в схеме.
- Новые страницы по умолчанию закрыты для всех, кроме `super_admin`.
"""

from __future__ import annotations

from datetime import UTC, datetime  # нужен в runtime для Mapped[datetime | None]

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
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base, SoftDeletableMixin, TimestampedMixin


# ─── Pivot tables (many-to-many) ──────────────────────────────────────

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "admin_user_id",
        Integer,
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        Integer,
        ForeignKey("admin_roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id",
        Integer,
        ForeignKey("admin_roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        Integer,
        ForeignKey("admin_permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

role_pages = Table(
    "role_pages",
    Base.metadata,
    Column(
        "role_id",
        Integer,
        ForeignKey("admin_roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "page_id",
        Integer,
        ForeignKey("admin_pages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

user_pages = Table(
    "user_pages",
    Base.metadata,
    Column(
        "admin_user_id",
        Integer,
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "page_id",
        Integer,
        ForeignKey("admin_pages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# ─── AdminGroup ───────────────────────────────────────────────────────


class AdminGroup(Base, SoftDeletableMixin):
    __tablename__ = "admin_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # tag-access RBAC (миграция 0052): потолок разрешённых батч-тегов для команды.
    # NULL = без ограничения (все теги). Задаёт super_admin.
    allowed_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)

    users: Mapped[list[AdminUser]] = relationship("AdminUser", back_populates="group")


# ─── AdminRole ────────────────────────────────────────────────────────


class AdminRole(Base, TimestampedMixin):
    __tablename__ = "admin_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Системная роль — нельзя удалить и (для super_admin) нельзя менять права.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # group_admin может назначать эту роль юзерам своей группы.
    # super_admin/group_admin сами через этот флаг недоступны.
    is_assignable_by_group_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    users: Mapped[list[AdminUser]] = relationship(
        "AdminUser",
        secondary=user_roles,
        back_populates="roles",
    )
    permissions: Mapped[list[AdminPermission]] = relationship(
        "AdminPermission",
        secondary=role_permissions,
        back_populates="roles",
    )
    pages: Mapped[list[AdminPage]] = relationship(
        "AdminPage",
        secondary=role_pages,
        back_populates="roles",
    )


# ─── AdminPermission ──────────────────────────────────────────────────


class AdminPermission(Base, TimestampedMixin):
    __tablename__ = "admin_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Каноническое имя: 'users.create', 'projects.delete', 'pages.assign'
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    roles: Mapped[list[AdminRole]] = relationship(
        "AdminRole",
        secondary=role_permissions,
        back_populates="permissions",
    )

    __table_args__ = (UniqueConstraint("resource", "action", name="ux_perm_resource_action"),)


# ─── AdminPage ────────────────────────────────────────────────────────


class AdminPage(Base, TimestampedMixin):
    __tablename__ = "admin_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    roles: Mapped[list[AdminRole]] = relationship(
        "AdminRole",
        secondary=role_pages,
        back_populates="pages",
    )
    users: Mapped[list[AdminUser]] = relationship(
        "AdminUser",
        secondary=user_pages,
        back_populates="direct_pages",
    )


# ─── AdminUser ────────────────────────────────────────────────────────


class AdminUser(Base, SoftDeletableMixin):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # tag-access RBAC (миграция 0052): персональный allowlist батч-тегов (сужение
    # внутри группы). NULL = наследует потолок группы. Эффективный набор =
    # пересечение user.allowed_tags ∩ group.allowed_tags (NULL = «все» на уровне).
    allowed_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)

    # Временный доступ (напр. поставщик доступов): после expires_at логин и токен
    # невалидны (проверяется в get_current_user / authenticate). is_temporary —
    # маркер таких аккаунтов для UI/фильтрации/cleanup. NULL expires_at = бессрочный.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    is_temporary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # sha256(magic-login-token) для входа по ссылке (режим передачи «magic-link»).
    # NULL — вход только по логину/паролю. Действует до expires_at.
    login_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Обратимо зашифрованный пароль ТОЛЬКО для временных supplier-аккаунтов —
    # чтобы super_admin мог посмотреть/скопировать его позже на странице
    # «Доступы поставщиков». Для обычных юзеров NULL (хранится лишь hash). См. 0049.
    temp_password_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)

    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    group: Mapped[AdminGroup | None] = relationship("AdminGroup", back_populates="users")
    roles: Mapped[list[AdminRole]] = relationship(
        "AdminRole",
        secondary=user_roles,
        back_populates="users",
    )
    direct_pages: Mapped[list[AdminPage]] = relationship(
        "AdminPage",
        secondary=user_pages,
        back_populates="users",
    )

    # ─── Helpers ──────────────────────────────────────────────────────

    @property
    def role_names(self) -> set[str]:
        return {r.name for r in self.roles if r.is_active}

    @property
    def is_expired(self) -> bool:
        """Истёк ли временный доступ. Бессрочный (expires_at IS NULL) — никогда."""
        return self.expires_at is not None and self.expires_at < datetime.now(UTC)

    @property
    def is_super_admin(self) -> bool:
        return "super_admin" in self.role_names

    @property
    def is_group_admin(self) -> bool:
        return "group_admin" in self.role_names

    def has_permission(self, code: str) -> bool:
        if self.is_super_admin:
            return True
        for role in self.roles:
            if not role.is_active:
                continue
            for perm in role.permissions:
                if perm.code == code or perm.code == "*":
                    return True
        return False

    def accessible_page_paths(self) -> set[str]:
        """Объединение страниц по ролям + индивидуально назначенных."""
        if self.is_super_admin:
            # Super_admin видит все активные страницы — определяется на стороне сервиса.
            return {"*"}

        paths: set[str] = set()
        for role in self.roles:
            if not role.is_active:
                continue
            for page in role.pages:
                if page.is_active:
                    paths.add(page.path)
        for page in self.direct_pages:
            if page.is_active:
                paths.add(page.path)
        return paths
