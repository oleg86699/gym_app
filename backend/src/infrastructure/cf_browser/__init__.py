"""CF Tier 3 — браузерный логин через Patchright для сайтов за Cloudflare Bot
Fight Mode (HTTP-клиент режется 403, cf_clearance не выдаётся). Браузер проходит
CF + логинится ОДИН раз → сессия (cookies+UA) в Redis-кеш; дальше вся работа
запросами (curl_cffi) с этой сессией. Браузер амортизирован — раз на сайт."""

from infrastructure.cf_browser.client import (
    browser_login_session,
    cache_session,
    get_cached_session,
    is_browser_available,
    post_via_session,
    replay_request,
)

__all__ = [
    "browser_login_session",
    "cache_session",
    "get_cached_session",
    "is_browser_available",
    "post_via_session",
    "replay_request",
]
