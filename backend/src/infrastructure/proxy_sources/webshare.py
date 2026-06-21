"""
Webshare.io proxy source.

Два режима в API:
- ?mode=direct — per-port residential / static (один уникальный IP per row)
- ?mode=backbone — rotating residential (один gateway, разные usernames)

Сначала пробуем direct (типичный сетап), если пусто — fallback на backbone.

Adapted from langgraph_ai_browser/app/proxies/sources/webshare.py.
"""

from __future__ import annotations

import httpx
import structlog

from infrastructure.proxy_sources import ImportedProxy

log = structlog.get_logger(__name__)

API_BASE = "https://proxy.webshare.io/api/v2"

DISPLAY_NAME = "Webshare.io"

FIELDS = [
    {
        "name": "api_key",
        "label": "API Key *",
        "type": "password",
        "required": True,
        "help": "dashboard.webshare.io → API → Copy.",
    },
    {
        "name": "allowed_countries",
        "label": "Allowed countries",
        "type": "textarea",
        "required": False,
        "default": "US,CA,GB,DE,FR,IT,ES,NL,SE,NO,FI,DK,PL,CZ,AU,NZ",
        "help": "ISO-2, через запятую. Пусто = все страны.",
    },
]


def _country_from_entry(entry: dict) -> str | None:
    cc = (entry.get("country_code") or entry.get("country") or "").strip().upper()
    return cc if len(cc) == 2 else None


async def _fetch_paginated(client: httpx.AsyncClient, url: str) -> list[dict]:
    out: list[dict] = []
    next_url: str | None = url
    while next_url:
        resp = await client.get(next_url)
        if resp.status_code == 401:
            raise PermissionError("Webshare API 401 — проверь API key")
        resp.raise_for_status()
        data = resp.json()
        out.extend(data.get("results") or [])
        next_url = data.get("next")
        if len(out) > 5000:
            log.warning("webshare.pagination.cap_5000")
            break
    return out


async def fetch(**opts) -> list[ImportedProxy]:
    api_key = (opts.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Webshare: API key пуст")

    raw_countries = opts.get("allowed_countries") or ""
    if isinstance(raw_countries, str):
        codes = {c.strip().upper() for c in raw_countries.split(",") if c.strip()}
    elif isinstance(raw_countries, (list, set, tuple)):
        codes = {str(c).strip().upper() for c in raw_countries if str(c).strip()}
    else:
        codes = set()
    allowed = codes or None

    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    out: list[ImportedProxy] = []
    skipped = 0

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        # Direct mode
        try:
            direct = await _fetch_paginated(client, f"{API_BASE}/proxy/list/?mode=direct&page_size=100")
            for e in direct:
                if not e.get("valid", True):
                    continue
                host = e.get("proxy_address") or e.get("host")
                port = e.get("port") or e.get("ports", {}).get("http")
                username = e.get("username")
                password = e.get("password")
                if not host or not port or not username:
                    continue
                country = _country_from_entry(e)
                if allowed is not None and (not country or country not in allowed):
                    skipped += 1
                    continue
                source_id = str(e.get("id") or f"{username}@{host}:{port}")
                out.append(ImportedProxy(
                    host=str(host),
                    port=int(port),
                    protocol="http",
                    username=str(username),
                    password=str(password) if password else None,
                    country=country,
                    source_id=source_id,
                    provider="webshare",
                    proxy_type="residential",
                ))
            if out or skipped > 0:
                log.info("webshare.direct.fetched", got=len(out), skipped=skipped)
                return out
        except PermissionError:
            raise
        except Exception as e:
            log.warning("webshare.direct.failed", error=str(e))

        # Backbone fallback
        try:
            bb = await _fetch_paginated(client, f"{API_BASE}/proxy/list/?mode=backbone&page_size=100")
            for e in bb:
                if not e.get("valid", True):
                    continue
                host = e.get("proxy_address") or "p.webshare.io"
                port = e.get("port") or 80
                username = e.get("username")
                password = e.get("password")
                if not username:
                    continue
                country = _country_from_entry(e)
                if allowed is not None and (not country or country not in allowed):
                    skipped += 1
                    continue
                source_id = str(e.get("id") or username)
                out.append(ImportedProxy(
                    host=str(host),
                    port=int(port),
                    protocol="http",
                    username=str(username),
                    password=str(password) if password else None,
                    country=country,
                    source_id=source_id,
                    provider="webshare",
                    proxy_type="residential",
                ))
            log.info("webshare.backbone.fetched", got=len(out), skipped=skipped)
        except PermissionError:
            raise
        except Exception as e:
            log.warning("webshare.backbone.failed", error=str(e))

    return out
