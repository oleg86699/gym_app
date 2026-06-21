"""
Random User-Agent для outbound HTTP requests (валидация / discovery).

Используем `fake-useragent` который раз в неделю обновляет список реальных
UA-строк с useragents.com. На случай если пакет недоступен offline или
update не прошёл — fallback на встроенный список.

Один UA per process — не имеет смысла, надо ротейтить per request чтобы
WAF/CF не видели подозрительно одинаковый трафик.
"""

from __future__ import annotations

import random

import structlog

log = structlog.get_logger(__name__)

# Curated fallback на случай если fake_useragent не работает или сеть down.
_FALLBACK_UAS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
]

_ua_provider = None


def _get_provider():
    global _ua_provider
    if _ua_provider is not None:
        return _ua_provider
    try:
        from fake_useragent import UserAgent

        _ua_provider = UserAgent(browsers=["chrome", "firefox", "safari", "edge"], os=["windows", "macos", "linux"])
        return _ua_provider
    except Exception as e:
        log.warning("useragents.fake_ua_init_failed", error=str(e))
        _ua_provider = False  # sentinel "не загружено"
        return False


def random_ua() -> str:
    """Случайный UA для одного запроса."""
    p = _get_provider()
    if p is False or p is None:
        return random.choice(_FALLBACK_UAS)
    try:
        ua = p.random
        if ua:
            return ua
    except Exception:
        pass
    return random.choice(_FALLBACK_UAS)
