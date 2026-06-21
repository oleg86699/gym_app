"""Юнит-тесты чистой логики размещения сквозных ссылок (без сети)."""
from infrastructure.wp_admin_client.client import WpAdminClient

C = WpAdminClient


# ─── _html_has_link: видим ли ссылку в HTML ──────────────────────────


def test_link_present_exact():
    assert C._html_has_link('<a href="https://t.co/x">a</a>', "https://t.co/x")


def test_link_present_without_scheme():
    assert C._html_has_link("visit t.co/x today", "https://t.co/x")


def test_link_present_amp_encoded():
    assert C._html_has_link('<a href="https://s.com/?a=1&amp;b=2">x</a>',
                            "https://s.com/?a=1&b=2")


def test_link_present_trailing_slash_variant():
    assert C._html_has_link('href="https://t.co/x"', "https://t.co/x/")


def test_link_absent():
    assert not C._html_has_link('<a href="https://other.com">a</a>', "https://t.co/x")


def test_link_empty_html():
    assert not C._html_has_link("", "https://t.co/x")


# ─── _first_internal_url: внутренняя страница для verify ──────────────


def test_first_internal_skips_admin_and_external():
    html = ('<a href="/wp-admin/x">adm</a>'
            '<a href="https://ext.com/y">ext</a>'
            '<a href="/about-us">about</a>')
    assert C._first_internal_url(html, "https://site.com") == "https://site.com/about-us"


def test_first_internal_skips_anchors_and_login():
    html = '<a href="#top">top</a><a href="/wp-login.php">login</a><a href="/blog/post">p</a>'
    assert C._first_internal_url(html, "https://site.com") == "https://site.com/blog/post"


def test_first_internal_absolute_same_host():
    html = '<a href="https://site.com/page-2">x</a>'
    assert C._first_internal_url(html, "https://site.com") == "https://site.com/page-2"


def test_first_internal_none_when_only_external():
    html = '<a href="https://other.com/a">x</a><a href="#frag">y</a>'
    assert C._first_internal_url(html, "https://site.com") is None


# ─── _link_html: экранирование ───────────────────────────────────────


def test_link_html_escapes_anchor():
    out = C._link_html("https://t.co/x", "My <b>Anchor</b> & co")
    assert "&lt;b&gt;" in out and "&amp; co" in out
    assert out.startswith('<a href="https://t.co/x">')


def test_link_html_escapes_url_amp():
    out = C._link_html("https://t.co/x?a=1&b=2", "anchor")
    assert "a=1&amp;b=2" in out
