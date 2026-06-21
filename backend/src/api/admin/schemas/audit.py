"""Pydantic-схемы для /admin/api/audit-log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ActorBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str | None = None


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    resource_type: str | None
    resource_id: int | None
    changes: dict[str, Any] | None
    ip: str | None
    user_agent: str | None
    created_at: datetime
    actor: ActorBrief | None = None


class AuditListResponse(BaseModel):
    items: list[AuditEntry]
    has_more: bool
