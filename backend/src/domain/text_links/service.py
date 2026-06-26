"""Разбор ссылок/анкоров из текста + disambiguation целевого бэклинка.

Для каждого текста (HTML) извлекаем все <a href>, нормализуем домены и по
списку доменов проекта (project_domains) определяем, какая ссылка «наша»
(целевой бэклинк). Правило (ключ — число РАЗНЫХ «наших» доменов):
  • ровно 1 «наш» домен      → авто, домен однозначен;
  • ≥2 разных «наших» домена → берём первую «нашу» (дефолт);
  • 0 «наших», но ссылки есть → needs_review (домен не в проекте);
  • ссылок нет вообще         → needs_review (нет ссылки).

«Наши» = домен ∈ project_domains; ссылки на источники (не-проектные) храним
как кандидаты, но в атрибуции игнорим.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString

from core.lang_detect import detect_language_from_html

# Теги, внутри которых не вставляем/не ищем анкор (скрипты, заголовки и т.п.)
_SKIP_PARENTS = {"a", "script", "style", "h1", "h2", "h3", "h4", "h5", "h6", "title"}


def normalize_domain(url_or_host: str | None) -> str | None:
    """Из URL или хоста → нормализованный домен (lowercase, без www, без порта).
    'https://www.Nawal.mx/path' → 'nawal.mx'. None если не распознали."""
    if not url_or_host:
        return None
    s = url_or_host.strip()
    if not s:
        return None
    # добавим схему, чтобы urlparse выделил netloc даже из голого хоста
    parsed = urlparse(s if "//" in s else f"//{s}", scheme="http")
    host = (parsed.netloc or parsed.path).strip().lower()
    host = host.split("@")[-1]      # выкидываем user:pass@
    host = host.split(":")[0]       # выкидываем :port
    host = host.split("/")[0]       # на всякий случай путь
    if host.startswith("www."):
        host = host[4:]
    # минимальная валидация: есть точка и допустимые символы
    if "." not in host or not re.match(r"^[a-z0-9.\-]+$", host):
        return None
    return host or None


@dataclass
class LinkCandidate:
    link: str
    anchor: str
    domain: str | None
    is_project_domain: bool

    def to_dict(self) -> dict:
        return {
            "link": self.link,
            "anchor": self.anchor,
            "domain": self.domain,
            "is_project_domain": self.is_project_domain,
        }


@dataclass
class AnalyzeResult:
    target_link: str | None = None
    target_anchor: str | None = None
    target_domain: str | None = None
    candidates: list[LinkCandidate] = field(default_factory=list)
    needs_review: bool = False
    review_reason: str | None = None   # 'domain_not_in_project' | 'no_link'
    lang: str | None = None

    def candidates_as_dicts(self) -> list[dict]:
        return [c.to_dict() for c in self.candidates]


def extract_links(html: str | None) -> list[tuple[str, str]]:
    """Все <a href> → [(href, anchor_text)]. Пустые/пустышечные href пропускаем."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        anchor = a.get_text(separator=" ", strip=True)
        out.append((href, anchor))
    return out


def _clean_url(u: str | None) -> str:
    """Срезать обрамляющие/висячие кавычки, угловые скобки и пробелы из URL.
    Чинит битый источник вида `https://x/"` (хвостовая кавычка) → `https://x/`."""
    if not u:
        return u or ""
    return u.strip().strip("\"'").strip().rstrip("\"'>").strip()


def sanitize_text_html(html: str | None) -> str | None:
    """Привести HTML статьи в порядок и починить ссылки.

    - Нормализуем разметку через BeautifulSoup: re-serialize чинит теги без
      кавычек/незакрытые (`<a href=https://x/">` → `<a href="https://x/">`).
    - Чистим URL в каждом <a href> (срезаем висячие кавычки/пробелы) — иначе
      публикуемый бэклинк битый (404), а verify не находит ссылку.

    Применяется при загрузке (unpack) ко ВСЕМ текстам и для починки уже залитых.
    Возвращает почищенный HTML (или исходное значение, если парсить нечего)."""
    if not html or "<" not in html:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html
    for a in soup.find_all("a"):
        href = a.get("href")
        if href is None:
            continue
        cleaned = _clean_url(href)
        if cleaned != href:
            if cleaned:
                a["href"] = cleaned
            else:
                del a["href"]
    return str(soup)


def _project_set(project_domains) -> set[str]:
    out: set[str] = set()
    for d in project_domains or []:
        nd = normalize_domain(d)
        if nd:
            out.add(nd)
    return out


def analyze_text(html: str | None, project_domains) -> AnalyzeResult:
    """Разобрать текст: язык + кандидаты-ссылки + выбор целевого бэклинка."""
    res = AnalyzeResult()
    res.lang = detect_language_from_html(html)

    pset = _project_set(project_domains)
    raw = extract_links(html)
    for href, anchor in raw:
        dom = normalize_domain(href)
        res.candidates.append(
            LinkCandidate(link=href, anchor=anchor, domain=dom,
                          is_project_domain=bool(dom and dom in pset))
        )

    ours = [c for c in res.candidates if c.is_project_domain]
    distinct_our_domains = {c.domain for c in ours if c.domain}

    if len(distinct_our_domains) >= 1:
        # 1 домен → однозначно; ≥2 → берём первую «нашу» (дефолт)
        target = ours[0]
        res.target_link = target.link
        res.target_anchor = target.anchor or None
        res.target_domain = target.domain
        res.needs_review = False
    elif res.candidates:
        # ссылки есть, но ни одна не «наша» — домен не добавлен в проект
        res.needs_review = True
        res.review_reason = "domain_not_in_project"
    else:
        # ссылок нет вообще
        res.needs_review = True
        res.review_reason = "no_link"

    return res


# ─── Инжект ссылки в готовый текст (reuse) ───────────────────────────


def _link_tag_html(link: str, anchor: str) -> str:
    return (f'<a href="{html_lib.escape(link, quote=True)}">'
            f'{html_lib.escape(anchor)}</a>')


def _text_nodes(soup: BeautifulSoup):
    """Текстовые узлы, пригодные для вставки (не внутри a/script/heading)."""
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        if not node.strip():
            continue
        if node.parent and node.parent.name in _SKIP_PARENTS:
            continue
        yield node


def _wrap_occurrence(soup: BeautifulSoup, phrase: str, link: str) -> bool:
    """Обернуть первое вхождение phrase (регистронезависимо) в текстовом узле
    в ссылку. Видимый текст = исходное вхождение (контекстно)."""
    if not phrase.strip():
        return False
    pat = re.compile(re.escape(phrase), re.IGNORECASE)
    for node in list(_text_nodes(soup)):
        s = str(node)
        m = pat.search(s)
        if not m:
            continue
        before, matched, after = s[:m.start()], m.group(0), s[m.end():]
        a = soup.new_tag("a", href=link)
        a.string = matched
        new_nodes: list = []
        if before:
            new_nodes.append(NavigableString(before))
        new_nodes.append(a)
        if after:
            new_nodes.append(NavigableString(after))
        node.replace_with(*new_nodes)  # bs4 4.10+: замена на несколько узлов
        return True
    return False


def _significant_words(anchor: str) -> list[str]:
    words = [w for w in re.split(r"\W+", anchor) if len(w) >= 4]
    words.sort(key=len, reverse=True)
    return words


def _insert_into_paragraph(soup: BeautifulSoup, a_tag) -> bool:
    """Фолбек: вставить ссылку в содержательный абзац (по тексту), а не в конец
    документа."""
    paras = [p for p in soup.find_all("p")
             if len(p.get_text(strip=True)) >= 40]
    if not paras:
        # нет <p> — добавим в конец body/документа
        target = soup.find("body") or soup
        sep = NavigableString(" ")
        target.append(sep)
        target.append(a_tag)
        return True
    # середина — менее заметно, чем первый/последний
    p = paras[len(paras) // 2]
    p.append(NavigableString(" "))
    p.append(a_tag)
    return True


def inject_link(html: str, link: str, anchor: str) -> str:
    """Reuse-инжект: вычистить старые <a> (текст сохранить), вставить новую
    ссылку «хорошей логикой»:
      1) если анкор встречается в тексте — обернуть это вхождение (контекстно);
      2) иначе — обернуть значимое слово из анкора;
      3) иначе — вставить ссылку в содержательный абзац.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    # 1. чистим старые ссылки (оставляем видимый текст)
    for a in soup.find_all("a"):
        a.unwrap()
    soup = BeautifulSoup(str(soup), "html.parser")  # нормализуем дерево после unwrap

    # 2. контекстная обёртка по анкору
    if anchor and _wrap_occurrence(soup, anchor, link):
        return str(soup)
    # 3. по значимому слову анкора
    for w in _significant_words(anchor or ""):
        if _wrap_occurrence(soup, w, link):
            return str(soup)
    # 4. фолбек — вставка в абзац
    a = soup.new_tag("a", href=link)
    a.string = anchor or link
    _insert_into_paragraph(soup, a)
    return str(soup)


# ─── Spintax-расшивка (детерминированный код, без AI) ────────────────

_SPIN_INNER = re.compile(r"\{([^{}]*)\}")


def spin(formula: str | None, rng: "random.Random | None" = None) -> str:
    """Развернуть spintax `{a|b|c}` в ОДИН вариант. Поддерживает вложенность
    (`{a|{b|c}}`) — раскрываем изнутри наружу. Без скобок → текст как есть.

    rng — опц. свой Random (для воспроизводимости/тестов); иначе глобальный.
    """
    import random as _random
    if not formula:
        return formula or ""
    r = rng or _random
    out = formula
    for _ in range(100_000):  # защита от битого ввода
        m = _SPIN_INNER.search(out)
        if not m:
            break
        options = m.group(1).split("|")
        out = out[:m.start()] + r.choice(options) + out[m.end():]
    return out
