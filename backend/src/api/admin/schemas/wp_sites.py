"""Pydantic-схемы для /admin/api/wp-sites + credentials."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ─── Credentials ─────────────────────────────────────────────────────


class WpCredentialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    login: str
    # Расшифрованный пароль — populated только при ?include_password=true
    # и только для super_admin (см. list_site_credentials_endpoint).
    password: str | None = None
    is_valid: bool
    last_validation_kind: str | None = None
    last_error_message: str | None = None
    can_xmlrpc: bool | None = None          # xmlrpc-эндпоинт жив
    can_post_via_xmlrpc: bool | None = None  # xmlrpc-ЛОГИН работает (можно постить)
    can_admin_login: bool | None = None
    can_create_users: bool | None = None
    admin_role: str | None = None
    # Provisioning — этот cred создан нами (provision-author)
    provisioned: bool = False
    provisioned_at: datetime | None = None
    provisioned_via: str | None = None
    error_counter: int
    last_validated_at: datetime | None
    amount_use: int
    last_used_at: datetime | None
    tags: list[str] | None = None
    note: str | None
    source_filename: str | None
    import_batch_id: int | None = None
    created_at: datetime


class CreateCredentialRequest(BaseModel):
    site_id: int
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=20)
    note: str | None = None


class UpdateCredentialRequest(BaseModel):
    login: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=20)
    note: str | None = None
    is_valid: bool | None = None


class BulkDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1)


# ─── Sites ────────────────────────────────────────────────────────────


class WpSiteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: str
    hint_path: str | None
    hint_port: int | None
    last_working_url: str | None
    last_working_at: datetime | None
    is_active: bool
    language: str | None
    language_detected_at: datetime | None = None
    note: str | None
    created_at: datetime
    # Site-level failure tracking — для отображения причины auto-disable + warning-ов
    consecutive_site_failures: int = 0
    last_site_failure_at: datetime | None = None
    last_site_failure_kind: str | None = None
    auto_disabled_at: datetime | None = None
    # Capability discovery (Tier 2/3 results, для UI)
    cf_protected: bool | None = None
    wp_version: str | None = None
    active_theme: str | None = None
    file_editing_disabled: bool | None = None
    homepage_is_static_page: bool | None = None
    homepage_page_id: int | None = None


class WpSiteListItem(WpSiteResponse):
    """Расширенная схема для list — с счётчиками credentials.
    `credentials_valid` — *подтверждённые* (kind ok/manual_valid + legacy
    validated). Свежеимпортированные default-valid в этот счётчик НЕ попадают."""
    credentials_total: int
    credentials_valid: int
    credentials_invalid: int = 0       # is_valid=False OR kind ∈ invalid_kinds
    credentials_pending: int = 0       # last_validated_at IS NULL
    credentials_transient: int = 0     # был ответ, но не conclusive
    credentials_provisioned: int = 0   # созданные нами (provision-author)
    # Channel-флаги (агрегат по cred сайта): работает ли XML-RPC / admin login.
    # True если хотя бы один cred подтвердил канал; False если хотя бы один
    # явно опроверг и никто не подтвердил; None — не проверяли.
    site_can_xmlrpc: bool | None = None          # xmlrpc-эндпоинт жив (не = логин работает)
    site_can_post_via_xmlrpc: bool | None = None  # xmlrpc-ЛОГИН работает (можно постить)
    site_can_admin: bool | None = None
    # max(credentials.last_validated_at) — когда сайт последний раз «трогали»
    last_credential_check_at: datetime | None = None
    # Сумма amount_use по живым cred сайта — сколько постов реально опубликовали
    total_uses: int = 0
    last_used_at: datetime | None = None


class WpSiteDetail(WpSiteResponse):
    """Детальная схема — с полным списком credentials."""
    credentials: list[WpCredentialResponse]


class WpSiteListResponse(BaseModel):
    items: list[WpSiteListItem]
    next_cursor: str | None = None
    has_more: bool
    total: int


class CreateSiteRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=255)
    hint_path: str | None = Field(default=None, max_length=200)
    hint_port: int | None = Field(default=None, ge=1, le=65535)
    note: str | None = None


class UpdateSiteRequest(BaseModel):
    domain: str | None = Field(default=None, min_length=3, max_length=255)
    hint_path: str | None = None
    hint_port: int | None = Field(default=None, ge=1, le=65535)
    is_active: bool | None = None
    note: str | None = None


# ─── Import + summary ────────────────────────────────────────────────


class ImportResultResponse(BaseModel):
    imported_credentials: int
    skipped_duplicate_credentials: int
    skipped_invalid_rows: int
    total_rows: int
    sites_created: int
    sites_touched: int


class SitePostEntry(BaseModel):
    """Один опубликованный пост с этого сайта — для analytics-таблицы."""
    text_item_id: int
    posting_run_id: int
    run_name: str
    run_creator_id: int | None = None         # кто создал run (юзер)
    run_creator_username: str | None = None
    project_id: int
    project_name: str
    credential_id: int | None
    credential_login: str | None
    posted_url: str | None
    posted_at: datetime | None
    text_title: str | None


class SiteAnalyticsResponse(BaseModel):
    """Аналитика по WP-сайту: посты, попытки, статистика."""
    site_id: int
    domain: str
    # Lifetime aggregates
    posts_total: int                              # всего успешных публикаций
    posts_24h: int                                # за последние 24ч
    posts_7d: int                                 # за последние 7 дней
    first_posted_at: datetime | None              # самая ранняя публикация
    last_posted_at: datetime | None               # самая последняя
    # Distinct projects used this site
    distinct_projects: int
    # Distinct credentials used to post (sometimes site has many admin accounts)
    distinct_credentials_used: int
    # Recent posts list (limit ~50, ordered by posted_at desc)
    recent_posts: list[SitePostEntry]


class PoolSummaryResponse(BaseModel):
    sites_total: int
    sites_active: int        # legacy: domain alive only
    sites_usable: int = 0    # is_active + есть ≥1 valid cred (постить можно)
    sites_unusable: int = 0  # domain off ИЛИ все cred invalid (постить нельзя)
    credentials_valid_rpc: int = 0    # valid через XML-RPC (Tier 1)
    credentials_valid_admin: int = 0  # valid через admin login (Tier 2)
    credentials_total: int
    credentials_valid: int
    credentials_invalid: int
    credentials_pending: int = 0      # никогда не валидировались
    credentials_transient: int = 0    # был ответ, но не conclusive (network/parked/timeout/etc.)
