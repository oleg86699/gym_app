"""
Pluggable proxy import sources (SOAX / Decodo / Webshare / ...).

Каждый модуль провайдера экспортирует:

    DISPLAY_NAME: str       — человекочитаемая метка в dropdown
    FIELDS: list[dict]      — схема формы для UI (динамический рендер)
    async def fetch(**opts) -> list[ImportedProxy]

UI один раз дёргает `list_source_metadata()`, рендерит provider-dropdown
и его поля. На submit POST /admin/api/proxies/import/{provider} передаёт
все form-fields как kwargs в fetch().

Чтобы добавить нового провайдера:
  1. Создать модуль с fetch / DISPLAY_NAME / FIELDS.
  2. Зарегистрировать в _discover_sources() ниже (одна строчка).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType


@dataclass
class ImportedProxy:
    """Нормализованная proxy-запись, возвращаемая source-модулем."""

    host: str
    port: int
    source_id: str  # provider-specific стабильный ID для upsert-а
    protocol: str = "http"
    username: str | None = None
    password: str | None = None
    country: str | None = None
    provider: str | None = None  # human-readable: soax / decodo / webshare
    proxy_type: str | None = None  # residential / mobile / datacenter / proxy


_MODULES: dict[str, ModuleType] = {}


def _discover_sources() -> dict[str, ModuleType]:
    if _MODULES:
        return _MODULES
    from infrastructure.proxy_sources import decodo, soax, webshare

    _MODULES["soax"] = soax
    _MODULES["decodo"] = decodo
    _MODULES["webshare"] = webshare
    return _MODULES


def get_source(name: str) -> Callable[..., Awaitable[list[ImportedProxy]]] | None:
    mods = _discover_sources()
    mod = mods.get(name)
    return getattr(mod, "fetch", None) if mod else None


def list_sources() -> list[str]:
    return sorted(_discover_sources().keys())


def list_source_metadata() -> list[dict]:
    """UI-ready метаданные: name, display_name, fields."""
    out: list[dict] = []
    for name in list_sources():
        mod = _MODULES[name]
        out.append({
            "name": name,
            "display_name": getattr(mod, "DISPLAY_NAME", name),
            "fields": getattr(mod, "FIELDS", []),
        })
    return out
