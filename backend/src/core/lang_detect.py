"""
Определение языка WP-сайта.

Трёхэтапная стратегия (от дешёвого к дорогому):
  1. `<html lang="ru">` — мгновенно, надёжно для правильных сайтов.
  2. `<meta http-equiv="content-language" content="ru">` или og:locale.
  3. `langdetect` по тексту body.

Возвращает ISO 639-1 ('en', 'ru', 'de'...) или None.

Используется из per-batch validator: один GET на homepage сайта в придачу
к XML-RPC проверке credential-а.
"""

from __future__ import annotations

import re

import httpx
import structlog
from bs4 import BeautifulSoup

from core.useragents import random_ua

log = structlog.get_logger(__name__)

# 6-12 chars maximum для нормализации (есть BCP 47 типа "en-US-x-private")
_LANG_RE = re.compile(r"^([a-zA-Z]{2,3})(?:[-_].*)?$")


def _normalize(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().lower()
    m = _LANG_RE.match(raw)
    if not m:
        return None
    code = m.group(1)
    # 3-letter ISO 639-3 → не нормализуем, отдаём как есть
    return code[:2] if len(code) >= 2 else None


def _detect_from_html_attr(soup: BeautifulSoup) -> str | None:
    html_tag = soup.find("html")
    if not html_tag:
        return None
    for attr in ("lang", "xml:lang"):
        val = html_tag.get(attr)
        if val:
            code = _normalize(str(val))
            if code:
                return code
    return None


def _detect_from_meta(soup: BeautifulSoup) -> str | None:
    # content-language
    m = soup.find("meta", attrs={"http-equiv": re.compile(r"^content-language$", re.I)})
    if m and m.get("content"):
        code = _normalize(str(m["content"]).split(",")[0])
        if code:
            return code
    # og:locale (e.g. "ru_RU")
    m = soup.find("meta", attrs={"property": "og:locale"})
    if m and m.get("content"):
        code = _normalize(str(m["content"]))
        if code:
            return code
    return None


def _gather_textual_content(soup: BeautifulSoup) -> str:
    """Собрать максимум осмысленного текста из страницы.

    Body — приоритет, но fallback на title + meta description + h1/h2 +
    JSON-LD когда body пустой (SPA). Иначе короткие JS-SPA страницы
    остаются без языка.
    """
    parts: list[str] = []
    body = soup.find("body")
    if body:
        text = body.get_text(separator=" ", strip=True)
        if text:
            parts.append(text)
    if not parts or sum(len(p) for p in parts) < 80:
        # Fallback: title, meta description, h1/h2, og:title/description
        title = soup.find("title")
        if title and title.string:
            parts.append(title.string.strip())
        for prop in ("description", "og:title", "og:description", "twitter:description"):
            m = soup.find("meta", attrs={"name": prop}) or soup.find("meta", attrs={"property": prop})
            if m and m.get("content"):
                parts.append(str(m["content"]).strip())
        for tag_name in ("h1", "h2"):
            for tag in soup.find_all(tag_name, limit=3):
                t = tag.get_text(separator=" ", strip=True)
                if t:
                    parts.append(t)
    joined = " ".join(parts).strip()
    # Финальный fallback: HTML-фрагмент без <body>/title (csv-direct, .txt) —
    # берём весь видимый текст документа.
    if len(joined) < 30:
        allt = soup.get_text(separator=" ", strip=True)
        if len(allt) > len(joined):
            joined = allt
    return joined


def _detect_from_text(soup: BeautifulSoup) -> str | None:
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # детерминизм
    except ImportError:
        return None
    text = _gather_textual_content(soup)
    if len(text) < 30:
        return None
    try:
        return _normalize(detect(text[:5000]))
    except Exception:
        return None


def detect_language_from_html(html: str | None) -> str | None:
    """Определить язык по готовому HTML-тексту (без скачивания) — для текстов,
    которые мы заливаем/генерируем. ISO-639-1 ('en'/'ru'/'de'/...) или None.

    Та же стратегия: html lang → meta → langdetect по тексту body.
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    return (
        _detect_from_html_attr(soup)
        or _detect_from_meta(soup)
        or _detect_from_text(soup)
    )


async def detect_language(
    homepage_url: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = 15.0,
) -> str | None:
    """
    Скачать homepage и определить язык. Возвращает ISO-639-1 ('en'/'ru'/'de'/...)
    либо None если не удалось.

    Безопасно вызывать со своим httpx-клиентом (для проксей/UA), либо без —
    создадим временный.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": random_ua()},
        )
    try:
        try:
            resp = await client.get(homepage_url)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.info("lang.fetch.failed", url=homepage_url, error=str(e))
            return None
        except Exception as e:
            log.warning("lang.fetch.error", url=homepage_url, error=str(e))
            return None
        # Не отбрасываем 4xx/5xx сразу — у многих error-pages всё равно
        # есть `<html lang="en-US">` или JSON-LD с языком. Парсим что есть.
        try:
            soup = BeautifulSoup(resp.text or "", "html.parser")
        except Exception:
            return None
        return (
            _detect_from_html_attr(soup)
            or _detect_from_meta(soup)
            or _detect_from_text(soup)
        )
    finally:
        if own_client and client is not None:
            await client.aclose()
