"""Disambiguation целевого бэклинка из текста (Фаза A)."""

from __future__ import annotations

from domain.text_links import analyze_text, normalize_domain

PD = ["nawal.mx", "footbal.net.ua"]  # домены проекта


def test_normalize_domain():
    assert normalize_domain("https://www.Nawal.mx/path?q=1") == "nawal.mx"
    assert normalize_domain("nawal.mx") == "nawal.mx"
    assert normalize_domain("http://user:pass@www.x.com:8080/a") == "x.com"
    assert normalize_domain("not a url") is None
    assert normalize_domain("") is None


def test_single_our_domain_auto():
    html = '<p>Текст <a href="https://nawal.mx/promo">Nawal</a> ещё текст</p>'
    r = analyze_text(html, PD)
    assert not r.needs_review
    assert r.target_domain == "nawal.mx"
    assert r.target_anchor == "Nawal"
    assert r.target_link == "https://nawal.mx/promo"


def test_our_plus_source_links_ignores_sources():
    # наша ссылка + ссылки на источники (не-проектные) → авто по нашей
    html = (
        '<a href="https://nawal.mx/">Nawal</a>'
        '<p>источники:</p>'
        '<a href="https://wikipedia.org/x">wiki</a>'
        '<a href="https://example.com/y">src</a>'
    )
    r = analyze_text(html, PD)
    assert not r.needs_review
    assert r.target_domain == "nawal.mx"
    # источники сохранены как кандидаты, но не «наши»
    assert any(c.domain == "wikipedia.org" and not c.is_project_domain for c in r.candidates)


def test_two_distinct_our_domains_takes_first():
    html = (
        '<a href="https://footbal.net.ua/a">фут</a>'
        '<a href="https://nawal.mx/b">naw</a>'
    )
    r = analyze_text(html, PD)
    assert not r.needs_review
    assert r.target_domain == "footbal.net.ua"  # первый «наш»


def test_links_but_none_ours_needs_review():
    html = '<a href="https://some-other.com/x">link</a>'
    r = analyze_text(html, PD)
    assert r.needs_review
    assert r.review_reason == "domain_not_in_project"
    assert len(r.candidates) == 1


def test_no_links_needs_review():
    html = "<p>Просто текст без единой ссылки</p>"
    r = analyze_text(html, PD)
    assert r.needs_review
    assert r.review_reason == "no_link"
    assert r.candidates == []


def test_multiple_links_same_our_domain_single():
    # несколько ссылок на ОДИН наш домен → 1 distinct → авто (не review)
    html = (
        '<a href="https://nawal.mx/a">A</a>'
        '<a href="https://www.nawal.mx/b">B</a>'
    )
    r = analyze_text(html, PD)
    assert not r.needs_review
    assert r.target_domain == "nawal.mx"
