"""Валидация бэклинка на опубликованном посте.

После успешного постинга WP возвращает post_id, но это НЕ гарантирует, что наш
бэклинк реально виден на живой странице (тема/плагин мог его вырезать, пост в
модерации и т.п.). Здесь — анонимный GET страницы поста (follow redirects →
реальный permalink) и проверка, что на ней есть ссылка на target_domain.

Критерий (по решению): достаточно домена ссылки — любой href на target_domain
(или его поддомен) на странице считается подтверждением.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _norm_domain(d: str | None) -> str:
    d = (d or "").strip().lower()
    if "://" in d:
        d = urlparse(d).netloc
    d = d.split("/")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def domain_in_hrefs(html: str, domain: str) -> bool:
    """Есть ли наш money-домен в КОДЕ страницы?

    По решению: проверяем НЕ парсингом href, а вхождением домена (как отдельного
    хост-токена) в исходник страницы. Так verify устойчив к битому HTML
    (`<a href=https://x/">` без кавычек и т.п.) и не плодит лишние посты при
    auto-валидации (не отбрасывает реально размещённую ссылку из-за кривой
    разметки).

    Границы: слева `(?<![\\w-])` — НЕ часть более длинного лейбла (nottrustpilot.com,
    my-trustpilot.com), но точку ПЕРЕД доменом разрешаем — иначе `www.trustpilot.com`
    (или любой поддомен money-домена) не матчился бы с нормализованным `trustpilot.com`
    и auto-verify давал ложный verify_failed (грайндил ран в 0). Справа `(?![\\w-])`
    — не ловить `trustpilot.company` и т.п."""
    d = _norm_domain(domain)
    if not html or not d:
        return False
    return re.search(r"(?<![\w-])" + re.escape(d) + r"(?![\w-])", html, re.IGNORECASE) is not None


async def verify_post_link(
    post_url: str, target_domain: str, *, proxy_url: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Анонимно фетчим страницу поста (follow redirects → реальный permalink),
    ищем ссылку на target_domain. Возвращаем (found, resolved_url).
    resolved_url — финальный URL после редиректов (или исходный при ошибке фетча)."""
    cb = uuid.uuid4().hex[:12]
    bu = post_url + ("&" if "?" in post_url else "?") + f"_glcb={cb}"
    status, text, final = 0, "", post_url
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=False,
            proxy=proxy_url, headers={"User-Agent": _UA},
        ) as c:
            r = await c.get(bu)
            status, text, final = r.status_code, r.text, str(r.url)
    except Exception as e:
        log.debug("post.verify.fetch_error", url=post_url, error=str(e))
    # CF/WAF-блок → curl_cffi (Chrome TLS) тем же proxy (иначе ложный провал)
    from infrastructure.cf_transport import cf_fetch, looks_cf_blocked
    if (not text) or looks_cf_blocked(status, text):
        alt = await cf_fetch(bu, proxy=proxy_url, timeout=int(timeout))
        if alt is not None and not looks_cf_blocked(alt[0], alt[1]):
            text = alt[1]
    found = domain_in_hrefs(text, target_domain)
    final = final.replace(f"&_glcb={cb}", "").replace(f"?_glcb={cb}", "")
    return found, final


async def verify_with_retries(
    post_url: str, target_domain: str, *, proxy_url: str | None = None,
    attempts: int = 1, delay: float = 3.0, timeout: float = 30.0,
) -> tuple[bool, str]:
    """verify_post_link с N попытками (auto-режим: пост мог не сразу попасть в
    кэш/индекс). Останавливаемся на первом успехе. Возвращаем (found, resolved_url)
    последней попытки."""
    found, resolved = False, post_url
    for i in range(max(1, attempts)):
        found, resolved = await verify_post_link(
            post_url, target_domain, proxy_url=proxy_url, timeout=timeout)
        if found:
            return True, resolved
        if i + 1 < attempts:
            await asyncio.sleep(delay)
    return found, resolved
