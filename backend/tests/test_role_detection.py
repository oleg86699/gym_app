"""Юнит-тесты эвристики роли по составу #adminmenu (cookie-only сигнал).

Проверяем лесенку administrator → editor → author → contributor → subscriber
и устойчивость к «тяжёлым» страницам, где меню лежит далеко в HTML.
"""
import pytest

from infrastructure.wp_admin_client.client import WpAdminClient
from infrastructure.wp_client.client import _parse_profile_role

_r = WpAdminClient._role_from_admin_menu


# ─── wp.getProfile role parsing (XML-RPC, Tier 1) ────────────────────


def _profile_resp(*roles: str) -> str:
    items = "".join(f"<value><string>{r}</string></value>" for r in roles)
    return (
        '<?xml version="1.0"?><methodResponse><params><param><value><struct>'
        '<member><name>roles</name><value><array><data>'
        f'{items}'
        '</data></array></value></member>'
        '</struct></value></param></params></methodResponse>'
    )


def test_profile_role_administrator():
    assert _parse_profile_role(_profile_resp("administrator")) == "administrator"


def test_profile_role_author():
    assert _parse_profile_role(_profile_resp("author")) == "author"


def test_profile_role_highest_of_many():
    # editor + author → берём старшую (editor)
    assert _parse_profile_role(_profile_resp("author", "editor")) == "editor"


def test_profile_role_custom_falls_back_to_first():
    # Кастомная роль (напр. WooCommerce 'seller') — не из приоритета → roles[0]
    assert _parse_profile_role(_profile_resp("seller")) == "seller"


def test_profile_role_fault_returns_none():
    fault = ('<?xml version="1.0"?><methodResponse><fault><value><struct>'
             '<member><name>faultCode</name><value><int>403</int></value></member>'
             '<member><name>faultString</name><value><string>denied</string></value></member>'
             '</struct></value></fault></methodResponse>')
    assert _parse_profile_role(fault) is None


def test_profile_role_empty_returns_none():
    assert _parse_profile_role(_profile_resp()) is None


def test_profile_role_garbage_returns_none():
    assert _parse_profile_role("<html>not xml-rpc</html>") is None


def _menu(*hrefs: str) -> str:
    """Минимальный wp-admin HTML с #adminmenu и заданными пунктами."""
    items = "".join(f'<li><a href="{h}">x</a></li>' for h in hrefs)
    return (
        '<body class="wp-admin"><div id="adminmenumain">'
        f'<ul id="adminmenu">{items}</ul></div>'
        '<div id="wpcontent">page body</div>'
        '<div id="wpfooter"></div></body>'
    )


def test_administrator_via_settings():
    assert _r(_menu("edit.php", "upload.php", "options-general.php")) == "administrator"


def test_administrator_via_plugins():
    assert _r(_menu("edit.php", "plugins.php")) == "administrator"


def test_administrator_via_users():
    assert _r(_menu("edit.php", "users.php")) == "administrator"


def test_administrator_via_appearance():
    assert _r(_menu("edit.php", "upload.php", "themes.php")) == "administrator"


def test_editor_pages_comments():
    # Pages + Comments, без Settings/Plugins/Users/Appearance → editor
    assert _r(_menu("edit.php", "upload.php", "edit.php?post_type=page",
                    "edit-comments.php")) == "editor"


def test_author_media_only():
    # Media (upload) но без Pages/Comments → author
    assert _r(_menu("edit.php", "upload.php")) == "author"


def test_author_with_comments_is_not_editor():
    # Регресс: меню Comments (edit-comments.php) рендерится при edit_posts, его
    # видят author/contributor. Author с Comments+Media (но без Pages) — это
    # author, НЕ editor. Раньше ложно классифицировался как editor.
    assert _r(_menu("edit.php", "upload.php", "edit-comments.php", "tools.php")) == "author"


def test_contributor_with_comments_is_not_editor():
    # Contributor: Posts + Comments, но без Media (upload_files) и без Pages.
    assert _r(_menu("edit.php", "edit-comments.php")) == "contributor"


def test_editor_requires_pages_not_just_comments():
    # editor определяется по Pages (edit_pages); только Comments недостаточно.
    assert _r(_menu("edit.php", "upload.php", "edit.php?post_type=page",
                    "edit-comments.php")) == "editor"


def test_contributor_posts_only():
    assert _r(_menu("edit.php")) == "contributor"


def test_subscriber_empty_menu():
    # Залогинены (есть #adminmenu), но никаких рабочих пунктов → subscriber
    assert _r(_menu("profile.php")) == "subscriber"


def test_no_admin_menu_returns_none():
    assert _r("<body><div>login form</div></body>") is None


def test_admin_menu_far_in_large_head():
    # Регресс: на тяжёлых сайтах плагины пишут сотни КБ в <head> ДО #adminmenu.
    # Меню всё равно должно распознаться (не обрезаем вход на 200KB).
    big_head = "<head>" + ("/* x */" * 60_000) + "</head>"  # ~400KB
    html = big_head + _menu("edit.php", "upload.php", "options-general.php")
    assert _r(html) == "administrator"


def test_page_content_links_dont_leak_into_menu():
    # Ссылка options-general.php в КОНТЕНТЕ (после #wpcontent) не должна
    # поднимать роль до administrator, если в самом меню её нет.
    html = (
        '<div id="adminmenumain"><ul id="adminmenu">'
        '<li><a href="edit.php">Posts</a></li></ul></div>'
        '<div id="wpcontent"><a href="options-general.php">link in body</a></div>'
        '<div id="wpfooter"></div>'
    )
    assert _r(html) == "contributor"
