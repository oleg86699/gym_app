"""
Cursor-based пагинация для list-эндпоинтов (см. ADR-011, категория А).

Соглашение: `?cursor=<base64>&limit=<n>`. Курсор opaque, клиент его не
парсит — только передаёт обратно для следующей страницы.
"""

from __future__ import annotations

import base64
import json
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# Безопасный максимум, чтобы не отдать миллион строк случайно
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class CursorParams(BaseModel):
    """Query-параметры пагинации."""

    cursor: str | None = Field(default=None, description="Opaque, из next_cursor предыдущего ответа")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    def after_id(self) -> int | None:
        """Декодировать cursor → id последнего элемента предыдущей страницы."""
        if not self.cursor:
            return None
        try:
            raw = base64.urlsafe_b64decode(self.cursor.encode("ascii")).decode("utf-8")
            payload = json.loads(raw)
            return int(payload["after_id"])
        except Exception:
            return None


def encode_cursor(after_id: int) -> str:
    """Закодировать после-id в opaque строку."""
    raw = json.dumps({"after_id": after_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


class PaginatedResponse(BaseModel, Generic[T]):
    """Стандартный ответ list-эндпоинта."""

    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
