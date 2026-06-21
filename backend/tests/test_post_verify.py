"""Валидация бэклинка на опубликованном посте: domain_in_hrefs + verify_post_link."""
from __future__ import annotations

import domain.postings.verify as vmod


def test_domain_in_hrefs():
    html = '<p>x <a href="https://londoncityofmusicexpo.ca/best.html">casino</a> y</p>'
    assert vmod.domain_in_hrefs(html, "londoncityofmusicexpo.ca")
    # передан полный URL — нормализуется до домена
    assert vmod.domain_in_hrefs(html, "https://londoncityofmusicexpo.ca/")
    # другой домен — нет
    assert not vmod.domain_in_hrefs(html, "example.com")
    # www и поддомен засчитываются
    assert vmod.domain_in_hrefs('<a href="http://www.foo.bar/">', "foo.bar")
    assert vmod.domain_in_hrefs('<a href="https://sub.foo.bar/p">', "foo.bar")
    # похожий, но другой домен — НЕ засчитываем
    assert not vmod.domain_in_hrefs('<a href="https://foobar.com/">', "foo.bar")
    # ссылка только в тексте (не в href) — не засчитываем
    assert not vmod.domain_in_hrefs("visit foo.bar today", "foo.bar")


class _FakeResp:
    def __init__(self, url: str, text: str, status: int = 200):
        self.url, self.text, self.status_code = url, text, status


def _fake_client(resp: _FakeResp):
    class _C:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return resp
    return _C


async def test_verify_post_link_found(monkeypatch):
    # редирект ?p=41810 → красивый permalink, на странице есть наша ссылка
    resp = _FakeResp("https://littleindiamarket.com/casino-review/",
                     '<a href="https://londoncityofmusicexpo.ca/">go</a>')
    monkeypatch.setattr(vmod.httpx, "AsyncClient", _fake_client(resp))
    found, resolved = await vmod.verify_post_link(
        "https://littleindiamarket.com/?p=41810", "londoncityofmusicexpo.ca")
    assert found is True
    assert resolved == "https://littleindiamarket.com/casino-review/"  # резолвленный permalink


async def test_verify_post_link_not_found(monkeypatch):
    resp = _FakeResp("https://littleindiamarket.com/?p=41810",
                     '<a href="https://someoneelse.com/">other</a>')
    monkeypatch.setattr(vmod.httpx, "AsyncClient", _fake_client(resp))
    found, _ = await vmod.verify_post_link(
        "https://littleindiamarket.com/?p=41810", "londoncityofmusicexpo.ca")
    assert found is False
