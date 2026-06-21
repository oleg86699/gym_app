"""
Decodo (Smartproxy) — два режима ввода:

- Manual: юзер вводит username из dashboard.decodo.com → генерим N port entries.
- API: юзер вводит api_key, мы дёргаем GET /v2/sub-users и для каждого
  активного subuser-а генерим port entries.

Adapted from langgraph_ai_browser/app/proxies/sources/decodo.py.
"""

from __future__ import annotations

import re

import httpx
import structlog

from infrastructure.proxy_sources import ImportedProxy

log = structlog.get_logger(__name__)

DISPLAY_NAME = "Decodo (Smartproxy)"
API_BASE = "https://api.decodo.com/v2"

FIELDS = [
    {
        "name": "username",
        "label": "Username (manual mode)",
        "type": "text",
        "required": False,
        "placeholder": "spk574zq6d",
        "help": "Из Proxy setup в dashboard. Префикс user- и suffix -sessionduration-N добавятся сами.",
    },
    {
        "name": "password",
        "label": "Residential password *",
        "type": "password",
        "required": True,
        "help": "Пароль из dashboard. API его не возвращает.",
    },
    {
        "name": "country",
        "label": "Country (optional)",
        "type": "text",
        "required": False,
        "placeholder": "us, gb, de",
        "help": "ISO-2 код. Mutually exclusive с Continent.",
    },
    {
        "name": "continent",
        "label": "Continent (optional)",
        "type": "text",
        "required": False,
        "placeholder": "eu/na/sa/as/af/oc",
        "help": "Игнорируется если задан Country.",
    },
    {
        "name": "api_key",
        "label": "API Key (auto-fetch mode)",
        "type": "password",
        "required": False,
        "help": "Из Account → API Keys. Нужен только если хочешь автомат-импорт всех sub-users.",
    },
    {
        "name": "gateway",
        "label": "Gateway host",
        "type": "text",
        "required": False,
        "default": "gate.decodo.com",
        "help": "Стандарт gate.decodo.com.",
    },
    {
        "name": "start_port",
        "label": "Start port",
        "type": "number",
        "required": False,
        "default": "10001",
        "help": "Первый sticky-session порт (обычно 10001).",
    },
    {
        "name": "count",
        "label": "Port count",
        "type": "number",
        "required": False,
        "default": "100",
        "help": "Количество sticky-endpoint-ов из твоего плана.",
    },
    {
        "name": "session_duration",
        "label": "Session duration (minutes)",
        "type": "number",
        "required": False,
        "default": "30",
        "help": "Sticky-длительность IP. Должно превышать timeout постинга.",
    },
]


_SUFFIX_RE = re.compile(
    r"-(country|city|state|session|sessionduration|zip|asn|continent)-.*$"
)


async def fetch(**opts) -> list[ImportedProxy]:
    api_key = (opts.get("api_key") or "").strip()
    username_manual = (opts.get("username") or "").strip()
    password = (opts.get("password") or "").strip()

    if not password:
        raise ValueError("Decodo: password обязателен (API не возвращает)")
    if not username_manual and not api_key:
        raise ValueError("Decodo: укажи Username (manual) или API Key (auto-fetch)")

    gateway = (opts.get("gateway") or "gate.decodo.com").strip()
    try:
        start_port = int(opts.get("start_port") or 10001)
    except (TypeError, ValueError):
        start_port = 10001
    try:
        count = int(opts.get("count") or 10)
    except (TypeError, ValueError):
        count = 10
    try:
        session_duration = int(opts.get("session_duration") or 30)
    except (TypeError, ValueError):
        session_duration = 30
    if not (1 <= count <= 1000):
        raise ValueError(f"Decodo: count 1..1000 (got {count})")

    country = (opts.get("country") or "").strip().lower()
    continent = (opts.get("continent") or "").strip().lower()
    geo_parts: list[str] = []
    if country:
        if len(country) != 2:
            raise ValueError(f"Decodo: country — 2 буквы (got {country!r})")
        geo_parts.append(f"country-{country}")
    elif continent:
        if continent not in {"eu", "na", "sa", "as", "af", "oc"}:
            raise ValueError(f"Decodo: continent eu/na/sa/as/af/oc (got {continent!r})")
        geo_parts.append(f"continent-{continent}")
    if session_duration > 0:
        geo_parts.append(f"sessionduration-{session_duration}")
    suffix = ("-" + "-".join(geo_parts)) if geo_parts else ""

    def _materialize(proxy_username: str, subuser_id: str) -> list[ImportedProxy]:
        rows: list[ImportedProxy] = []
        for idx in range(count):
            port = start_port + idx
            rows.append(
                ImportedProxy(
                    host=gateway,
                    port=port,
                    protocol="http",
                    username=proxy_username,
                    password=password,
                    country=country.upper() if country else None,
                    source_id=f"decodo:{subuser_id}:{gateway}:{port}",
                    provider="decodo",
                    proxy_type="residential",
                )
            )
        return rows

    # Manual mode
    if username_manual:
        base = _SUFFIX_RE.sub("", username_manual)
        if not base.startswith("user-"):
            base = f"user-{base}"
        return _materialize(f"{base}{suffix}", base)

    # API mode — пробуем разные header-стили
    auth_variants = [
        ("ApiKey", {"Authorization": f"ApiKey {api_key}"}, {}),
        ("Basic", {"Authorization": f"Basic {api_key}"}, {}),
        ("Bearer", {"Authorization": f"Bearer {api_key}"}, {}),
        ("Token", {"Authorization": f"Token {api_key}"}, {}),
        ("query", {}, {"api-key": api_key}),
    ]
    out: list[ImportedProxy] = []
    last_401 = ""

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = None
        accepted = ""
        for label, hdrs, qs in auth_variants:
            try:
                resp = await client.get(
                    f"{API_BASE}/sub-users",
                    params={"service_type": "residential_proxies", **qs},
                    headers={"Accept": "application/json", **hdrs},
                )
            except Exception as exc:
                raise RuntimeError(f"Decodo API request failed: {exc}") from exc
            if resp.status_code == 401:
                if not last_401:
                    last_401 = (resp.text or "")[:200]
                continue
            accepted = label
            break

        if resp is None or resp.status_code == 401:
            raise PermissionError(
                f"Decodo API 401 для всех auth-вариантов. Response: {last_401!r}. "
                "Проверь что ключ из Public API (не Web Scraping) и план включает residential_proxies."
            )
        if resp.status_code == 403:
            raise PermissionError("Decodo API 403 — ключ без доступа к sub-users")
        resp.raise_for_status()
        log.info("decodo.api.authenticated", variant=accepted)

        data = resp.json()
        subusers = data if isinstance(data, list) else (data.get("data") or data.get("results") or [])
        if not isinstance(subusers, list):
            raise RuntimeError(f"Decodo: unexpected payload shape: {type(data).__name__}")

        active = [su for su in subusers if (su.get("status") or "active").lower() == "active"]
        for su in active:
            raw = str(su.get("username") or "").strip()
            if not raw:
                continue
            base = _SUFFIX_RE.sub("", raw)
            if not base.startswith("user-"):
                base = f"user-{base}"
            subuser_id = str(su.get("id") or base)
            out.extend(_materialize(f"{base}{suffix}", subuser_id))

    return out
