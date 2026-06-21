"""Pydantic-схемы для /admin/api/proxies."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProxyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    protocol: str
    host: str
    port: int
    username: str | None
    # password НЕ отдаём наружу
    country: str | None
    provider: str | None
    proxy_type: str | None
    note: str | None
    is_active: bool
    status: str
    last_checked_at: datetime | None
    last_check_error: str | None
    external_ip: str | None
    isp: str | None
    asn: str | None
    source: str
    source_id: str | None
    created_at: datetime


class ProxyListResponse(BaseModel):
    items: list[ProxyResponse]
    total: int


class CreateProxyRequest(BaseModel):
    protocol: str = Field(default="http", pattern="^(http|https|socks5)$")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=500)
    country: str | None = Field(default=None, max_length=10)
    proxy_type: str | None = Field(default=None, max_length=20)
    provider: str | None = Field(default=None, max_length=100)
    note: str | None = None


class BulkAddRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1024 * 1024)


class BulkAddResult(BaseModel):
    parsed: int
    inserted: int
    invalid: list[str] = Field(default_factory=list)


class ImportRequest(BaseModel):
    # Произвольные поля от UI form: все как-есть в fetch(**opts)
    opts: dict


class ImportResult(BaseModel):
    created: int
    updated: int
    total_in_db: int


class CheckResult(BaseModel):
    ok: bool
    external_ip: str | None = None
    country: str | None = None
    isp: str | None = None
    asn: str | None = None
    proxy_type: str | None = None
    error: str | None = None


class SourceFieldSchema(BaseModel):
    name: str
    label: str
    type: str  # text | password | number | textarea | select
    required: bool = False
    default: str | None = None
    placeholder: str | None = None
    help: str | None = None
    options: list[str] | None = None  # для select


class SourceMetadata(BaseModel):
    name: str
    display_name: str
    fields: list[SourceFieldSchema]


class ProviderStat(BaseModel):
    source: str
    count: int


class PoolStats(BaseModel):
    """Сводка active+working прокси для выбора в New run dialog.
    Возвращается из /admin/api/proxies/pools."""
    all_active: int
    providers: dict[str, int]    # {"webshare": 2550, ...}
