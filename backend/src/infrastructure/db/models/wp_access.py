"""
WordPress access — два уровня:

- WpSite — один на каждый реальный WP-сайт (по bare-домену).
  Хранит: domain, hint_path/hint_port (если WP не в корне),
  last_working_url (кеш discovery), is_active.

- WpCredential — учётка (login + password) для конкретного сайта.
  Одному сайту может принадлежать много credentials (разные admin-аккаунты).

Правило «1 site = 1 проект» обеспечивается через `project_wp_used.site_id`,
а не credential_id — иначе 10 credentials к одному сайту дали бы 10 постов
с одного домена, что нам не нужно.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base, SoftDeletableMixin


class WpSite(Base, SoftDeletableMixin):
    __tablename__ = "wp_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # bare host: lowercase, без www., без path, без port, без протокола
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Подсказки для discovery если WP живёт не в корне или на нестандартном порту.
    # Опциональные; задаются вручную в UI когда стандартные кандидаты не сработали.
    hint_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hint_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Кеш discovered XML-RPC endpoint. Заполняется при первом успешном
    # постинге через любую credential этого сайта. См. infrastructure/wp_client/.
    last_working_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_working_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Когда сайт последний раз УСПЕШНО приняли пост — для LRU-отбора кандидатов
    # (_pick_candidate_sites: last_used_at NULLS FIRST, random()). Ровный делёж
    # бэклинков по пулу; NULL = ещё ни разу не постили (берётся первым).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    language_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Site-class failure tracking — для авто-выключения мёртвых доменов.
    # Считаем подряд идущие network/server_error/site_not_found/xmlrpc_disabled
    # по любой credential этого сайта. Любой успех ИЛИ AUTH-ошибка (т.е. сайт
    # ответил XML-RPC-ом и сам сказал «нет») сбрасывает счётчик в 0.
    consecutive_site_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    last_site_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_site_failure_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    auto_disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ─── Site-level capability discovery (заполняется Tier 2 / Tier 3) ──
    # None = ещё не проверяли, False = проверили и нет, True = да.
    cf_protected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    wp_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_theme: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Detected via «File editing is disabled» в HTML / DISALLOW_FILE_EDIT
    file_editing_disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Settings → Reading: show_on_front=page (для Stage 3 link placement)
    homepage_is_static_page: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    homepage_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    credentials = relationship(
        "WpCredential", back_populates="site", cascade="all, delete-orphan"
    )


class WpCredential(Base, SoftDeletableMixin):
    __tablename__ = "wp_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("wp_sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    login: Mapped[str] = mapped_column(String(255), nullable=False)
    # TODO(stage 3): Fernet-encryption
    password: Mapped[str] = mapped_column(String(500), nullable=False)

    # Валидация конкретной credential — может быть невалидной даже если site жив
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # cred_status — STORED generated column (миграция 0025). Единый источник
    # истины: 'valid' | 'invalid' | 'transient' | 'pending'. Read-only из ORM
    # (БД вычисляет сама). Все агрегаты/фильтры/MV должны читать ЕГО, а не
    # пересобирать логику из is_valid+kind+can_admin_login по отдельности.
    cred_status: Mapped[str] = mapped_column(
        String(16),
        Computed(
            "CASE "
            "WHEN is_valid IS FALSE OR last_validation_kind IN "
            "('auth_invalid','permission_denied','manual_invalid') THEN 'invalid' "
            "WHEN last_validated_at IS NULL THEN 'pending' "
            "WHEN last_validation_kind IN ('ok','manual_valid') "
            "OR last_validation_kind IS NULL OR can_admin_login IS TRUE THEN 'valid' "
            "ELSE 'transient' END",
            persisted=True,
        ),
        nullable=False,
    )

    # Cooldown — после AUTH-ошибки не считаем новых ошибок до этого момента
    # (защита от 25 параллельных корутин, попавших на 1 cred за минуту).
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Результат последней валидации — для UI badge-а и диагностики.
    # ok / auth_invalid / permission_denied / network / server_error / xmlrpc_disabled / site_not_found / unknown / cooldown_skipped
    last_validation_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Статистика использования
    amount_use: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Метаинформация
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Был String(100). Перевели на TEXT[] для multi-tag (одну кред помечаем
    # несколькими меткам). Миграция 0017 backfill-ит старый tag как [tag].
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Привязка к batch-у импорта (nullable для legacy / manual entries)
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("wp_import_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ─── Tier-capability matrix (per cred × site) ───────────────────
    # None = ещё не проверяли, False = проверили и не работает, True = работает.
    # Заполняется валидатором (batch и future ondemand) и eager-marking-ом
    # из реального posting worker-а.
    can_xmlrpc: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_admin_login: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_post_via_xmlrpc: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_post_via_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_edit_pages: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_edit_themes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_edit_widgets: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # create_users cap (из REST users/me) — нужен для provision-author
    can_create_users: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # WP role/capability: administrator | editor | author | contributor | subscriber
    admin_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Когда последний раз гоняли Tier 2 (admin form-login + capability probes)
    last_admin_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ─── Provisioning (наш собственный созданный пользователь) ───────
    # provisioned=True → этот cred мы СОЗДАЛИ сами на сайте (provision-author),
    # а не импортировали. Подсвечиваем в UI отдельным цветом.
    provisioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # каким admin-кредом мы создали этого юзера (для аудита/трассировки)
    provisioned_by_cred_id: Mapped[int | None] = mapped_column(
        ForeignKey("wp_credentials.id", ondelete="SET NULL"), nullable=True
    )
    # 'form' (wp-admin/user-new.php) | 'rest' (/wp-json/wp/v2/users)
    provisioned_via: Mapped[str | None] = mapped_column(String(16), nullable=True)

    site = relationship("WpSite", back_populates="credentials")
