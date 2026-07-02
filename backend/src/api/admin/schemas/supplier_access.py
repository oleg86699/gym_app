"""Schemas for /admin/api/supplier-access."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateSupplierAccessRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    ttl_hours: int = Field(default=24 * 7, ge=1, le=24 * 90)
    # Способ передачи доступа поставщику.
    handover: str = Field(default="password", pattern="^(password|link)$")
    # Батчи, к которым сразу открыть доступ поставщику (переназначаем владельца).
    # null/[] → без предоткрытых батчей (поставщик грузит свои).
    batch_ids: list[int] | None = None


class SupplierAccessCreatedResponse(BaseModel):
    """Показывается ОДИН раз после создания."""
    user_id: int
    username: str
    expires_at: datetime
    note: str | None = None
    handover: str
    # Заполнено в зависимости от handover (одно из двух):
    password: str | None = None     # handover="password"
    magic_url: str | None = None    # handover="link"
    login_url: str                  # куда заходить (страница логина)
    granted_batches: int = 0        # сколько батчей открыто поставщику сразу


class SupplierAccessItem(BaseModel):
    """Строка в списке активных/истёкших доступов поставщиков."""
    user_id: int
    username: str
    note: str | None = None
    is_active: bool
    expires_at: datetime | None = None
    is_expired: bool
    created_at: datetime
    last_login_at: datetime | None = None
    handover: str  # "link" если есть login_token_hash, иначе "password"
    # Расшифрованный пароль supplier-аккаунта (эндпоинт только для super_admin).
    # None у старых аккаунтов, созданных до фичи (пароль был только в hash).
    password: str | None = None


class SupplierAccessListResponse(BaseModel):
    items: list[SupplierAccessItem]
