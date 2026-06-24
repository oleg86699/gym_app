"""Schemas for /admin/api/batches."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WpBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    tag: str | None
    note: str | None
    cost_total: float | None = None
    cost_currency: str | None = None
    source_filename: str | None
    status: str
    total_credentials: int
    duplicate_credentials: int
    # Live counters — считаются из wp_credentials per-request (не из БД-кеша),
    # чтобы отображать актуальное состояние во время валидации.
    valid_count: int
    valid_xmlrpc_count: int = 0  # Tier 1 OK (rpc work)
    valid_admin_count: int = 0   # Tier 2 OK (rpc dead but admin login works)
    invalid_count: int
    transient_count: int
    pending_count: int = 0
    provisioned_count: int = 0  # сколько кредов в батче получили наш аккаунт (provision)
    pause_requested: bool
    validation_started_at: datetime | None
    validation_finished_at: datetime | None
    created_by_user_id: int | None
    created_at: datetime


class WpBatchListResponse(BaseModel):
    items: list[WpBatchResponse]


class CreateBatchImportResult(BaseModel):
    batch_id: int
    parsed_rows: int
    sites_created: int
    sites_touched: int
    credentials_new: int
    credentials_duplicate: int
    skipped_invalid_rows: int
    # Сразу после импорта запущен полный цикл (валидация full + provision)?
    validation_started: bool = False


class ValidateBatchRequest(BaseModel):
    scope: str = Field(default="all", pattern="^(all|invalid|pending)$")
    # None → берём DEFAULT_VALIDATION_CONCURRENCY из настроек сервера (per-server).
    concurrency: int | None = Field(default=None, ge=1, le=50)
    proxy_id: int | None = None
    detect_language: bool = True
    # Уровень валидации:
    #   light — только XML-RPC (Tier 1), самый быстрый.
    #   medium — + admin form-login (Tier 2) для случаев XML-RPC disabled
    #            или network errors. ~3x запросов.
    #   full — medium + capability probes (theme-editor / widgets / pages /
    #            wp_version / role). ~6-7 запросов на cred.
    # По умолчанию валидация ВСЕГДА идёт полным циклом (full).
    level: str = Field(default="full", pattern="^(light|medium|full)$")
    # После валидации создать наши аккаунты на admin-сайтах батча
    provision_after: bool = False
    provision_role: str = Field(default="author", pattern="^(author|editor|administrator)$")


class ProvisionRequest(BaseModel):
    """Запрос на создание наших аккаунтов (provision-author)."""
    role: str = Field(default="author", pattern="^(author|editor|administrator)$")
    concurrency: int = Field(default=4, ge=1, le=20)


class BatchCredEntry(BaseModel):
    """Один credential батча — для детальной таблицы /batches/[id]."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    domain: str
    language: str | None
    language_detected_at: datetime | None = None
    login: str
    tags: list[str] | None = None
    is_valid: bool
    last_validated_at: datetime | None
    last_validation_kind: str | None = None
    last_error_message: str | None = None
    error_counter: int
    last_error_at: datetime | None
    error_cooldown_until: datetime | None
    last_used_at: datetime | None
    amount_use: int
    created_at: datetime
    # Расшифрованный пароль — заполняется ТОЛЬКО для super_admin при явном
    # `include_password=true` query-param. Для остальных = None. Логируется
    # в audit при выдаче.
    password: str | None = None
    # Capability matrix (Tier 1+2 discovery)
    can_xmlrpc: bool | None = None
    can_admin_login: bool | None = None
    can_post_via_xmlrpc: bool | None = None
    can_post_via_admin: bool | None = None
    can_create_users: bool | None = None
    admin_role: str | None = None
    last_admin_check_at: datetime | None = None
    # Provisioning — этот cred создан нами (provision-author)
    provisioned: bool = False
    provisioned_at: datetime | None = None
    provisioned_via: str | None = None
    # Через этот admin-кред мы создали наш аккаунт на сайте (provisioned-кред
    # лежит вне батча, поэтому показываем пометку на исходном креде).
    provisioned_here: bool = False
    # При filter='duplicates' — id «оригинального» batch'а где сейчас живёт cred.
    # Для обычных rows это всегда current batch_id.
    import_batch_id: int | None = None


class BatchCredListResponse(BaseModel):
    items: list[BatchCredEntry]
    has_more: bool


class ForceCredStatusRequest(BaseModel):
    """Ручной override: пометить cred валидным или невалидным без перевалидации."""
    is_valid: bool
