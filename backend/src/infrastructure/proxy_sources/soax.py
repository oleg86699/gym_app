"""
SOAX (https://soax.com) — residential / mobile / ISP proxies.

Поддерживаем manual-режим (как и для Decodo): юзер вводит package_id +
password из дашборда SOAX, мы генерируем N sticky-port endpoints.

Формат username SOAX: `package-{ID}-sessionid-{rnd}-country-{cc}-sessionlength-{N}`
- `sessionid` уникален per port (sticky session)
- опционально: country / city / state / asn для гео-таргетинга
- sessionlength в МИНУТАХ (как и Decodo's sessionduration)

Gateway по типу пакета:
- residential: proxy.soax.com:5000
- mobile:      proxy.soax.com:9000
- isp:         proxy.soax.com:5000 (тот же что residential, port одинаковый)

SOAX в отличие от Decodo не имеет одного фиксированного списка sticky-портов —
один gateway port обслуживает все sessions, sticky-привязка через sessionid в
username. Поэтому генерируем N разных sessionid → N разных «sticky» entries
которые формально все идут через один TCP-порт.

Docs: https://helpcenter.soax.com/en/articles/6228586-using-soax
"""

from __future__ import annotations

import secrets
import string

from infrastructure.proxy_sources import ImportedProxy

DISPLAY_NAME = "SOAX"

FIELDS = [
    {
        "name": "package_id",
        "label": "Package ID *",
        "type": "text",
        "required": True,
        "placeholder": "190123",
        "help": "ID пакета из SOAX dashboard (Packages → твой пакет → 'package' в username).",
    },
    {
        "name": "password",
        "label": "Package password *",
        "type": "password",
        "required": True,
        "help": "Пароль пакета из SOAX dashboard. API его не возвращает — вводится один раз.",
    },
    {
        "name": "product",
        "label": "Product type",
        "type": "select",
        "required": False,
        "default": "residential",
        "options": ["residential", "mobile", "isp"],
        "help": "Определяет порт gateway: residential/isp = 5000, mobile = 9000.",
    },
    {
        "name": "country",
        "label": "Country (optional)",
        "type": "text",
        "required": False,
        "placeholder": "us, gb, de, fr...",
        "help": "ISO-2 код. Пусто = global pool. Пины каждую sticky session к стране.",
    },
    {
        "name": "city",
        "label": "City (optional)",
        "type": "text",
        "required": False,
        "placeholder": "newyork (без пробелов)",
        "help": "Имя города в lowercase без пробелов. Работает только с country.",
    },
    {
        "name": "asn",
        "label": "ASN (optional)",
        "type": "text",
        "required": False,
        "placeholder": "AS7922",
        "help": "Привязка к конкретному ISP по ASN. Расширенный таргетинг.",
    },
    {
        "name": "gateway",
        "label": "Gateway host",
        "type": "text",
        "required": False,
        "default": "proxy.soax.com",
        "help": "Дефолт proxy.soax.com — менять если у тебя dedicated gateway.",
    },
    {
        "name": "count",
        "label": "Sticky sessions count",
        "type": "number",
        "required": False,
        "default": "100",
        "help": "Сколько разных sessionid сгенерировать (каждый = отдельная sticky IP в pool-е).",
    },
    {
        "name": "session_length",
        "label": "Session length (minutes)",
        "type": "number",
        "required": False,
        "default": "30",
        "help": "Сколько минут SOAX держит sticky IP. Должно превышать timeout постинга.",
    },
]


# Маппинг product → (default port, proxy_type для модели)
_PRODUCT_PORT = {
    "residential": 5000,
    "isp": 5000,
    "mobile": 9000,
}
_PRODUCT_TYPE = {
    "residential": "residential",
    "isp": "residential",  # ISP — это static residential по сути
    "mobile": "mobile",
}


def _random_session_id(n: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


async def fetch(**opts) -> list[ImportedProxy]:
    package_id = str(opts.get("package_id") or "").strip()
    password = str(opts.get("password") or "").strip()
    if not package_id:
        raise ValueError("SOAX: package_id обязателен")
    if not password:
        raise ValueError("SOAX: password обязателен (API его не возвращает)")

    product = (opts.get("product") or "residential").strip().lower()
    if product not in _PRODUCT_PORT:
        raise ValueError(f"SOAX: product должен быть residential/mobile/isp (got {product!r})")

    gateway = (opts.get("gateway") or "proxy.soax.com").strip()
    try:
        count = int(opts.get("count") or 100)
    except (TypeError, ValueError):
        count = 100
    if not (1 <= count <= 1000):
        raise ValueError(f"SOAX: count в диапазоне 1..1000 (got {count})")

    try:
        session_length = int(opts.get("session_length") or 30)
    except (TypeError, ValueError):
        session_length = 30

    country = (opts.get("country") or "").strip().lower()
    if country and len(country) != 2:
        raise ValueError(f"SOAX: country — 2 буквы (got {country!r})")
    city = (opts.get("city") or "").strip().lower()
    asn = (opts.get("asn") or "").strip()

    port = _PRODUCT_PORT[product]
    ptype = _PRODUCT_TYPE[product]

    # Собираем suffix частей username по соглашению SOAX:
    # package-{id}-sessionid-{rnd}-country-{cc}-city-{x}-asn-{x}-sessionlength-{N}
    common_suffix_parts: list[str] = []
    if country:
        common_suffix_parts.append(f"country-{country}")
    if city:
        common_suffix_parts.append(f"city-{city}")
    if asn:
        common_suffix_parts.append(f"asn-{asn}")
    if session_length > 0:
        common_suffix_parts.append(f"sessionlength-{session_length}")
    common_suffix = ("-" + "-".join(common_suffix_parts)) if common_suffix_parts else ""

    rows: list[ImportedProxy] = []
    for _idx in range(count):
        sid = _random_session_id()
        username = f"package-{package_id}-sessionid-{sid}{common_suffix}"
        rows.append(
            ImportedProxy(
                host=gateway,
                port=port,
                protocol="http",
                username=username,
                password=password,
                country=country.upper() if country else None,
                provider="soax",
                proxy_type=ptype,
                # Стабильный ID per session (для upsert при ре-импорте этим же
                # package_id + product + sessionid). Но т.к. sessionid у нас
                # рандомный, повторный import создаст НОВЫЕ rows. Это намеренно:
                # SOAX не имеет stable per-session endpoints, sessions создаются
                # под нагрузкой. Если нужно «обновить» — сначала Remove all, потом
                # Re-import.
                source_id=f"soax:{package_id}:{product}:{sid}",
            )
        )
    return rows
