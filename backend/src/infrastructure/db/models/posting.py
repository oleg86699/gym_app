"""
Posting models: PostingRun, TextItem, ProjectWpUsed, RunArtifact.

См. plans/02_stage2_posting_core.md и ADR-003 (денормализованные счётчики),
ADR-002 (хранение файлов в MinIO + метаданные в БД), ADR-011 (индексы).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base, SoftDeletableMixin, TimestampedMixin


class PostingRunPriority(StrEnum):
    """Приоритет run-а в очереди Celery.

    Маппится на Celery `priority` поле: low=9, normal=5, high=0
    (в Celery МЕНЬШЕ значение = ВЫШЕ приоритет для Redis-брокера).
    """
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# Маппинг бизнес-приоритета → Celery priority value
CELERY_PRIORITY_MAP: dict[str, int] = {
    PostingRunPriority.HIGH.value: 0,
    PostingRunPriority.NORMAL.value: 5,
    PostingRunPriority.LOW.value: 9,
}


class PostingRunStatus(StrEnum):
    DRAFT = "draft"               # создан, но текст ещё не загружен
    UNPACKING = "unpacking"       # архив распаковывается в text_items
    READY = "ready"               # тексты распакованы, ждёт ручного Start от юзера
    SCHEDULED = "scheduled"       # ждёт scheduled_for
    QUEUED = "queued"             # отправлен в Celery, ждёт воркера
    RUNNING = "running"           # воркер обрабатывает
    PAUSED = "paused"             # пользовательская пауза
    DONE = "done"                 # все text_items в финальном статусе
    FAILED = "failed"             # системная ошибка
    NEED_MORE_ADMINS = "need_more_admins"   # закончились валидные админки
    CANCELLED = "cancelled"       # пользовательская отмена
    INTERRUPTED = "interrupted"   # воркер умер, recovery job заметил
    NEEDS_REVIEW = "needs_review" # остались задачи без данных (ссылка/домен) — ждут дозаполнения


class TextItemStatus(StrEnum):
    PENDING = "pending"      # ждёт воркера
    GENERATING = "generating"  # идёт генерация текста этого айтема (claim, не задвоить)
    POSTING = "posting"      # воркер сейчас обрабатывает
    POSTED = "posted"        # успех — есть posted_url
    FAILED = "failed"        # все попытки исчерпаны
    SKIPPED = "skipped"      # пропущен (например, дубль контента)
    NEEDS_REVIEW = "needs_review"  # нужны доп. данные (целевая ссылка/домен) — в постинг не идёт
    AWAITING_GENERATION = "awaiting_generation"  # ждёт генератор тела (gen-воркер, C2)
    AWAITING_REVIEW = "awaiting_review"          # ждёт ручного ревью/спина (manual) — в постинг не идёт


class RunArtifactKind(StrEnum):
    SOURCE_ARCHIVE = "source_archive"   # оригинальный .zip
    RESULT_CSV = "result_csv"           # CSV с результатами


class RunTaskType(StrEnum):
    """Тип run-а — что именно ставим на сайт."""
    POST = "post"                    # обычный пост со ссылкой (как сейчас)
    SITEWIDE_LINK = "sitewide_link"  # сквозная ссылка (footer/header на всех страницах)
    HOMEPAGE_LINK = "homepage_link"  # ссылка с главной страницы


class LinkPlacedVia(StrEnum):
    """Каким методом размещена сквозная/homepage ссылка."""
    WIDGET = "widget"            # custom_html виджет в footer/sidebar (REST)
    NAV_MENU = "nav_menu"        # пункт меню custom-link (REST)
    FSE_TEMPLATE = "fse_template"  # блок в footer template-part (блочные темы)
    FOOTER_PHP = "footer_php"    # правка footer.php (file-edit, резерв)
    MU_PLUGIN = "mu_plugin"      # must-use плагин (нужна запись файлов, последний)
    # homepage-типы:
    HOME_PAGE = "home_page"          # ссылка в контенте статической главной (page_on_front)
    HOME_TEMPLATE = "home_template"  # блок в FSE-шаблоне главной (front-page/home)


# ─── PostingRun ───────────────────────────────────────────────────────


class PostingRun(Base, SoftDeletableMixin):
    __tablename__ = "posting_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=PostingRunStatus.DRAFT)
    # Тип run-а: post (default) | sitewide_link | homepage_link
    task_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RunTaskType.POST.value,
        server_default=RunTaskType.POST.value,
    )

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Drip-feed: на сколько дней «размазать» постинг всех text_items этого run-а.
    # 0 = постить всё сразу. >0 = каждому item проставляется not_before, разнесённый
    # по окну [старт, старт+N дней]; run постит due-порцию и засыпает до следующей.
    spread_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ── C1 Content Engine: режим источника контента ──────────────────
    # upload_txt | csv_direct | csv_campaign
    content_source: Mapped[str] = mapped_column(
        String(20), default="upload_txt", nullable=False
    )
    # gen_per_row | gen_per_post | reuse (только для csv_campaign), иначе NULL
    content_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # auto | manual (manual ждёт ручного старта после подготовки/ревью)
    run_mode: Mapped[str] = mapped_column(String(10), default="auto", nullable=False)
    # параметры генерации: prompt_template_id, ai_model_id, language, default_keyword…
    gen_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Окно публикации (для аудита). Снимок app_settings на момент создания run-а:
    # каждому посту воркер ставит случайную дату внутри [publish_from, publish_to].
    # Оба NULL = посты публикуются текущим моментом без размазывания.
    publish_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    publish_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Concurrency и timeout — берутся из app_settings (общие для всех run-ов).
    # Колонки оставлены для аудита: реальное значение которое использовал воркер.
    concurrency: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    # Сколько раз один WP-сайт можно использовать в ЭТОЙ задаче (per-run, не
    # per-project). 1 = классическое «1 сайт = 1 пост». Воркер читает live —
    # можно поднять на ходу/после завершения, чтобы добрать сайты. См. 0040.
    max_posts_per_site: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Приоритет: low/normal/high. См. PostingRunPriority + CELERY_PRIORITY_MAP.
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PostingRunPriority.NORMAL.value
    )
    # Канал постинга: auto (default), xmlrpc_only, admin_only. См. миграцию 0021.
    posting_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="auto", server_default="auto"
    )
    # Валидация бэклинка на опубликованном посте (post-типы). См. миграцию 0043.
    #   mark — 1 GET, отметка ✓/✗, пост done в любом случае.
    #   auto — перепост на др. сайт, пока ссылка не подтвердится (иначе не done).
    post_verify: Mapped[str] = mapped_column(
        String(8), nullable=False, default="mark", server_default="mark"
    )
    # Селектор пула прокси (см. миграцию 0022). nullable для back-compat —
    # старые runs использовали proxy_id (single). Worker сначала смотрит на
    # selector, потом на proxy_id, потом direct.
    proxy_selector: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Управление воркером
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Денормализованные счётчики (ADR-003) — атомарный UPDATE col=col+1
    total_texts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Перепроверка проставленных бэклинков — отдельная фоновая задача (TaskIQ),
    # запускается ВРУЧНУЮ после завершения постинга. Перепроверяет ссылки, которые
    # уже были валидны (link_verified=true), и обновляет их отметку. Видна в
    # глобальной очереди как отдельный (фиолетовый) тип задач. См. миграцию 0048.
    #   link_check_status: NULL | queued | running | done
    link_check_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    link_check_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    link_check_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    link_check_valid: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    link_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Прокси (этап 3) — пока nullable заглушка без FK
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Storage key оригинального .zip в MinIO (через RunArtifact тоже видно)
    source_archive_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Relationships
    project = relationship("Project", foreign_keys=[project_id])
    creator = relationship("AdminUser", foreign_keys=[created_by])

    __table_args__ = (
        Index("ix_posting_runs_project_status", "project_id", "status"),
        Index("ix_posting_runs_status_scheduled", "status", "scheduled_for"),
    )


# ─── TextItem ─────────────────────────────────────────────────────────


class TextItem(Base, TimestampedMixin):
    __tablename__ = "text_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    posting_run_id: Mapped[int] = mapped_column(
        ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Файл в MinIO
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TextItemStatus.PENDING)

    # Заполняется при успехе
    posted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Конкретная credential через которую запостили (для аудита)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("wp_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Site — для дедупа «1 site = 1 проект» и для аналитики
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("wp_sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── Link-типы (sitewide_link / homepage_link) ──────────────────
    # Для post-типа NULL. link_url/link_anchor — что ставим; placed_via/ref —
    # как и где разместили (для verify, идемпотентности и удаления).
    link_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_anchor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    placed_via: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # id/маркер для поиска и удаления: widget_id | menu_item_id | template_id#block | file-marker
    placement_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # URL-ы, где подтвердили наличие ссылки (анонимно)
    verified_urls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Валидация бэклинка на посте (post-типы, run.post_verify): NULL=не проверяли,
    # true=ссылка на target_domain есть на странице поста, false=проверили, нет.
    link_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Сколько раз проверяли/перепостили ради подтверждения (auto-режим).
    verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Drip-feed: не брать задачу в работу раньше этого момента. NULL = можно сразу.
    # Заполняется при spread_days>0 (разнесение по окну). Воркер фильтрует
    # pending по (not_before IS NULL OR not_before <= now()).
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ─── Фаза A: разбор ссылок + язык ───────────────────────────────
    # Нормализованный домен целевой ссылки (из link_url) — для аналитики по доменам.
    target_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Извлечённые из текста ссылки-кандидаты (для UI выбора при needs_review):
    # [{"link":..,"anchor":..,"domain":..,"is_project_domain":bool}, ...]
    link_candidates: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Язык текста (langdetect на заливке / селектор генератора). Пока только хранение.
    lang: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # B1: ссылка на тело в единой библиотеке texts (источник истины тела текста).
    # NULL → старые items, тело которых ещё в MinIO (storage_key) до бэкфилла.
    text_id: Mapped[int | None] = mapped_column(
        ForeignKey("texts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # gen-контекст строки файла для пер-айтем (ре)генерации csv_campaign:
    # {keyword, language, link, anchor, ...}. Нужен, чтобы по кнопке заново
    # отрендерить промпт. NULL для не-сгенерированных задач. См. content-pipeline.
    gen_row: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    credential = relationship("WpCredential", foreign_keys=[credential_id])
    text = relationship("Text", foreign_keys=[text_id])
    site = relationship("WpSite", foreign_keys=[site_id])

    __table_args__ = (
        Index("ix_text_items_run_status", "posting_run_id", "status"),
        Index("ix_text_items_project_status", "project_id", "status"),
    )


# ─── ProjectWpUsed ────────────────────────────────────────────────────


class ProjectWpUsed(Base, TimestampedMixin):
    """История «использованных» сайтов per проект.

    Количество разрешённых публикаций на один сайт задаёт ЗАДАЧА
    (`posting_runs.max_posts_per_site`, default 1). Воркер при выборе сайта
    сравнивает COUNT(*) этой таблицы (per project+site) с лимитом задачи.

    Дедуп по site_id (не по credential_id): 10 credentials к одному сайту
    разделяют общий счётчик.
    """

    __tablename__ = "project_wp_used"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("wp_sites.id", ondelete="CASCADE"), nullable=False
    )
    posting_run_id: Mapped[int] = mapped_column(
        ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False
    )
    text_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("text_items.id", ondelete="SET NULL"), nullable=True
    )
    # Какая именно credential выполнила постинг — для аудита
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("wp_credentials.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # Non-unique индекс для быстрого COUNT(*) в _pick_candidate_sites.
        Index("ix_project_wp_used_project_site", "project_id", "site_id"),
        Index("ix_project_wp_used_run", "posting_run_id"),
    )


# ─── RunArtifact ──────────────────────────────────────────────────────


class RunArtifact(Base, TimestampedMixin):
    """Файл-артефакт прогона: оригинальный архив, CSV результата и т.п."""

    __tablename__ = "run_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    posting_run_id: Mapped[int] = mapped_column(
        ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
