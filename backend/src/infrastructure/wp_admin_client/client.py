"""WpAdminClient — Tier 2 валидатор/постер через wp-admin (httpx, без браузера).

Flow:
    1. GET https://domain/wp-login.php
       — копируем cookies (wordpress_test_cookie ставится автоматически на GET)
       — детектируем CF challenge / Wordfence shield в HTML
    2. POST https://domain/wp-login.php  с form-data:
         log=USER, pwd=PASS, wp-submit=Log In, redirect_to=/wp-admin/, testcookie=1
       — успех: 302 на /wp-admin/ + cookie wordpress_logged_in_*
       — fail:  200 + <div id="login_error">...</div>
    3. GET /wp-admin/  — проверяем что попали в админку (#wpadminbar или #adminmenu).
       Если редирект обратно на /wp-login.php — что-то не так с cookies / nonce.

Capability probes (если AdminCapabilities.probe_full=True):
    - /wp-admin/post-new.php   → can_post_via_admin (есть форма с #title)
    - /wp-admin/edit.php?post_type=page → can_edit_pages
    - /wp-admin/theme-editor.php → can_edit_themes (НЕТ «File editing is disabled»)
    - /wp-admin/widgets.php    → can_edit_widgets
    - /wp-admin/profile.php    → admin_role (из data-* атрибутов)
    - / (homepage)             → wp_version (meta generator), CF detection

Безопасность:
    - Все POST идут с разумным таймаутом и follow_redirects=True
    - verify=False (как в основном XML-RPC клиенте — у дешёвого WP-хостинга
      часто истёкшие сертификаты)
    - User-Agent рандомизируется на каждый запрос (через `random_ua()`)

Не делаем:
    - Не пытаемся обойти CloudFlare сами — это работа Tier 3 (FlareSolverr).
      Просто детектируем и помечаем `site.cf_protected=True`.
    - Не делаем capcha-solving.
    - Не запускаем JavaScript (нет браузера) — некоторые login-страницы с
      кастомным URL или JS-rendered формами пропустятся.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from html import escape
from typing import TYPE_CHECKING

import httpx
import structlog
from lxml import html as lxml_html

from infrastructure.wp_client.client import _classify_fault

if TYPE_CHECKING:
    from infrastructure.db.models import WpSite

log = structlog.get_logger(__name__)


# ─── Result enums ────────────────────────────────────────────────────


class AdminLoginKind(StrEnum):
    OK = "ok"
    AUTH_INVALID = "auth_invalid"           # текст «Incorrect username or password» (на любом языке)
    PERMISSION_DENIED = "permission_denied"  # WP пустил по login, но не показывает админку
    CF_CHALLENGE = "cf_challenge"           # Cloudflare блокирует доступ к login странице
    RATE_LIMITED = "rate_limited"           # 429 или security plugin throttling (Limit-Login etc.)
    LOGIN_DISABLED = "login_disabled"       # /wp-login.php → 404 / редирект на кастомный URL
    SITE_NOT_FOUND = "site_not_found"       # 404 на /wp-login.php
    NETWORK = "network"                     # timeout / connection error
    SERVER_ERROR = "server_error"           # 5xx
    PARKED = "parked"                       # parking-page / suspended / cgi-sys
    UNKNOWN = "unknown"


def _xmlrpc_kind_to_admin_kind(xkind_value: str) -> AdminLoginKind:
    """Конвертация ErrorKind из wp_client (XML-RPC) в наш AdminLoginKind.
    Используется когда мы прогнали HTML-response через общий
    `_classify_html_response`. Возвращает best-fit вариант."""
    mapping = {
        "rate_limited": AdminLoginKind.RATE_LIMITED,
        "cf_challenge": AdminLoginKind.CF_CHALLENGE,
        "parked": AdminLoginKind.PARKED,
        "server_error": AdminLoginKind.SERVER_ERROR,
        "site_not_found": AdminLoginKind.SITE_NOT_FOUND,
        "network": AdminLoginKind.NETWORK,
        # BROKEN_ENDPOINT для login.php не идеально — мы ждали login form,
        # а пришёл какой-то HTML. Маркируем как UNKNOWN — пусть админ
        # руками разберётся, или Tier 3 попробует.
        "broken_endpoint": AdminLoginKind.UNKNOWN,
    }
    return mapping.get(xkind_value, AdminLoginKind.UNKNOWN)


class AdminPostKind(StrEnum):
    OK = "ok"
    NO_PERMISSION = "no_permission"         # нет прав на edit_posts (subscriber/contributor)
    NONCE_FAIL = "nonce_fail"               # POST вернул «security check failed»
    NOT_LOGGED_IN = "not_logged_in"         # cookies протухли посреди flow
    UNKNOWN = "unknown"


# ─── Dataclasses ─────────────────────────────────────────────────────


@dataclass
class AdminCapabilities:
    """Что эта cred может в wp-admin. Заполняется опциональными пробами после login."""
    can_post_via_admin: bool | None = None
    can_edit_pages: bool | None = None
    can_edit_themes: bool | None = None
    can_edit_widgets: bool | None = None
    admin_role: str | None = None
    # Site-level (общие на сайт, не на cred — но узнаём после login)
    wp_version: str | None = None
    active_theme: str | None = None
    file_editing_disabled: bool | None = None
    homepage_is_static_page: bool | None = None
    homepage_page_id: int | None = None


@dataclass
class LoginOutcome:
    error: AdminLoginKind
    # Cookies зашитые в httpx.AsyncClient после успешного login — для дальнейших запросов
    # (мы не возвращаем их явно, клиент сохраняет в своём cookie jar)
    login_redirect_url: str | None = None        # куда WP редиректнул после login
    error_message: str | None = None             # raw текст из #login_error если был
    capabilities: AdminCapabilities = field(default_factory=AdminCapabilities)

    @property
    def success(self) -> bool:
        return self.error == AdminLoginKind.OK


@dataclass
class PostViaAdminOutcome:
    error: AdminPostKind
    post_id: int | None = None
    posted_url: str | None = None
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.error == AdminPostKind.OK


class AdminUserCreateKind(StrEnum):
    OK = "ok"
    NO_PERMISSION = "no_permission"   # не админ / нет create_users
    NONCE_FAIL = "nonce_fail"         # security check failed
    NOT_LOGGED_IN = "not_logged_in"   # cookies протухли
    DUPLICATE = "duplicate"           # username/email уже есть
    UNKNOWN = "unknown"


@dataclass
class UserCreateOutcome:
    """Результат создания нового WP-пользователя (provision-author)."""
    error: AdminUserCreateKind
    user_id: int | None = None
    username: str | None = None
    role: str | None = None
    via: str | None = None            # 'rest' | 'form'
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.error == AdminUserCreateKind.OK


class LinkPlaceKind(StrEnum):
    OK = "ok"
    NO_NONCE = "no_nonce"            # не достали REST-nonce (нет admin-сессии)
    NO_METHOD = "no_method"         # ни один метод не дал verified-ссылку
    NOT_LOGGED_IN = "not_logged_in"
    ERROR = "error"


@dataclass
class LinkPlacementProbe:
    """Снимок сайта для выбора метода размещения сквозной/homepage ссылки."""
    is_block_theme: bool = False
    sidebars: list = field(default_factory=list)       # [{"id","name"}]
    footer_sidebar_id: str | None = None
    nav_menus: list = field(default_factory=list)       # [{"id","name"}]
    footer_menu_id: int | None = None
    footer_template_part_id: str | None = None          # FSE
    show_on_front: str | None = None                    # 'posts' | 'page' (для homepage-типа)
    page_on_front: int | None = None
    error: str | None = None


@dataclass
class LinkPlaceOutcome:
    error: LinkPlaceKind
    placed_via: str | None = None       # widget | nav_menu | fse_template
    placement_ref: str | None = None    # id/маркер для verify+remove
    verified_urls: list | None = None
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.error == LinkPlaceKind.OK


# ─── Detection patterns ──────────────────────────────────────────────

_CF_MARKERS = (
    "checking your browser before accessing",
    "just a moment...",
    "/cdn-cgi/challenge-platform/",
    "cloudflare ray id",
    "cf-error-details",
    'data-cf-beacon',
    "challenges.cloudflare.com",
)

# JS-интерстициалы anti-bot (часто отдают HTTP 200, не 403/503!): подгружают
# JS, ставят cookie, потом редиректят. httpx их не исполнит → нужен Tier 3 (FS).
# Эти маркеры триггерят CF_CHALLENGE даже на 200.
_JS_CHALLENGE_MARKERS = (
    "verifying that you are not a robot",
    "verifying you are not a robot",
    "verifying that you are human",
    "verifying you are human",
    "verify you are human",
    "please wait while we verify",
    "please wait while your request is being verified",
    "ddos protection by",
    "dd-guard",
    "one moment please",
    "enable javascript and cookies to continue",
)

# Wordfence «You have been blocked» / Sucuri / iThemes блокирующие страницы
_BLOCKED_PAGE_MARKERS = (
    "your access to this site has been limited",
    "wordfence",
    "you have been blocked",
    "blocked by your security",
    "this site has been temporarily disabled",
)

# «File editing is disabled» в theme-editor.php / plugin-editor.php
_FILE_EDIT_DISABLED_MARKERS = (
    "file editing is disabled",
    "file editing has been disabled",
    "редактирование файлов отключено",  # RU
)


def _is_cf_challenge(body: str, status: int) -> bool:
    lower = (body or "")[:8192].lower()
    # CF/Sucuri/Wordfence — обычно на 403/503
    if status in (403, 503):
        for m in _CF_MARKERS:
            if m in lower:
                return True
    # JS anti-bot интерстициал — часто HTTP 200, проверяем на любом статусе.
    for m in _JS_CHALLENGE_MARKERS:
        if m in lower:
            return True
    return False


def _is_blocked_page(body: str) -> bool:
    lower = (body or "")[:8192].lower()
    return any(m in lower for m in _BLOCKED_PAGE_MARKERS)


# ─── Main client ─────────────────────────────────────────────────────


class WpAdminClient:
    """
    Один инстанс на cred (он же запоминает session cookies). Можно
    переиспользовать на один и тот же сайт для post + capability probes
    без повторного login.

    Использует общий внешний httpx.AsyncClient — но с собственным
    cookie jar (передаётся при создании). Так мы можем переиспользовать
    proxy/user-agent настройки родительского клиента.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout_seconds: int = 30,
        proxy_url: str | None = None,
    ):
        # Оборачиваем httpx-клиент в CfHttpxAdapter: при CF/WAF TLS-блоке
        # запросы (login + REST + капы) прозрачно ретраятся через curl_cffi
        # (Chrome TLS) с персистентным cookie-jar тем же proxy. Drop-in — все
        # self._client.get/post/delete остаются без изменений.
        from infrastructure.cf_transport import CfHttpxAdapter

        self._proxy_url = proxy_url
        self._client = CfHttpxAdapter(client, proxy_url=proxy_url)
        self._timeout = timeout_seconds

    @staticmethod
    def _site_base_url(site: "WpSite") -> str:
        """Базовый URL сайта (https преимущественно). Используем
        last_working_url если есть — там точно WP, дешевле."""
        if site.last_working_url:
            from urllib.parse import urlparse
            parsed = urlparse(site.last_working_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        return f"https://{site.domain}"

    # ─── Login ────────────────────────────────────────────────────

    async def login(self, site: "WpSite", login: str, password: str) -> LoginOutcome:
        """
        Form-login через /wp-login.php.

        Возвращает LoginOutcome. На успехе session cookies сохранены в self._client
        — можно сразу использовать `create_post()` / `probe_capabilities()`.
        """
        base = self._site_base_url(site)
        login_url = f"{base}/wp-login.php"

        # 1. GET /wp-login.php — собираем initial cookies (wordpress_test_cookie)
        try:
            resp = await self._client.get(
                login_url, timeout=self._timeout, follow_redirects=True
            )
        except httpx.TimeoutException:
            return LoginOutcome(error=AdminLoginKind.NETWORK, error_message="GET timeout")
        except httpx.NetworkError as e:
            return LoginOutcome(error=AdminLoginKind.NETWORK, error_message=str(e))
        except Exception as e:
            log.warning("wp_admin.get_login.error", url=login_url, error=str(e))
            return LoginOutcome(error=AdminLoginKind.UNKNOWN, error_message=str(e))

        if _is_cf_challenge(resp.text, resp.status_code):
            return LoginOutcome(
                error=AdminLoginKind.CF_CHALLENGE,
                error_message="Cloudflare challenge на /wp-login.php",
            )
        if resp.status_code == 404:
            return LoginOutcome(
                error=AdminLoginKind.LOGIN_DISABLED,
                error_message="/wp-login.php → 404 (возможно кастомный login URL)",
            )
        # 4xx (кроме 404) и 5xx: используем общий HTML-классификатор от XML-RPC
        # клиента — он различает 429 / CF / parking / 5xx / broken HTML.
        if resp.status_code >= 400:
            from infrastructure.wp_client.client import _classify_html_response
            xkind, xmsg = _classify_html_response(resp.status_code, resp.text or "")
            return LoginOutcome(
                error=_xmlrpc_kind_to_admin_kind(xkind.value),
                error_message=xmsg or f"HTTP {resp.status_code} на GET /wp-login.php",
            )

        # Parking-page detection (re-use from XML-RPC client via lazy import)
        from infrastructure.wp_client.client import _looks_parked
        if _looks_parked(str(resp.url), resp.text):
            return LoginOutcome(error=AdminLoginKind.PARKED,
                                error_message=f"{site.domain} parked / suspended")

        if _is_blocked_page(resp.text):
            # Wordfence / iThemes — обычно это rate-limit
            return LoginOutcome(
                error=AdminLoginKind.RATE_LIMITED,
                error_message="Security plugin block (Wordfence/Sucuri/iThemes)",
            )

        # 2. POST credentials
        form_data = {
            "log": login,
            "pwd": password,
            "wp-submit": "Log In",
            "redirect_to": f"{base}/wp-admin/",
            "testcookie": "1",
        }
        try:
            login_resp = await self._client.post(
                login_url,
                data=form_data,
                timeout=self._timeout,
                follow_redirects=False,  # детектим 302 явно
                headers={"Referer": login_url},
            )
        except httpx.TimeoutException:
            return LoginOutcome(error=AdminLoginKind.NETWORK, error_message="POST timeout")
        except httpx.NetworkError as e:
            return LoginOutcome(error=AdminLoginKind.NETWORK, error_message=str(e))

        # Успешный login: 302 → /wp-admin/ + cookies wordpress_logged_in_*
        if login_resp.status_code in (301, 302, 303):
            loc = login_resp.headers.get("Location", "")
            cookies_set = login_resp.headers.get_list("set-cookie")
            has_logged_in = any("wordpress_logged_in" in c for c in cookies_set)

            if has_logged_in or "/wp-admin" in loc:
                # 3. Validate с GET /wp-admin/  — точно ли пустили
                ok, msg = await self._verify_admin_access(base)
                if ok:
                    return LoginOutcome(
                        error=AdminLoginKind.OK,
                        login_redirect_url=loc,
                    )
                return LoginOutcome(
                    error=AdminLoginKind.PERMISSION_DENIED,
                    error_message=msg or "редирект OK, но /wp-admin/ не доступен",
                )
            # 302 на /wp-login.php?... = WP проводит ещё один login round
            # обычно из-за неправильных cookies / testcookie не передался
            if "wp-login.php" in loc:
                return LoginOutcome(
                    error=AdminLoginKind.AUTH_INVALID,
                    error_message=f"WP редиректнул обратно на login: {loc[:120]}",
                )
            return LoginOutcome(
                error=AdminLoginKind.UNKNOWN,
                error_message=f"unexpected redirect to {loc[:120]}",
            )

        # 200 с #login_error — типичный fail case
        if login_resp.status_code == 200:
            err_text = self._parse_login_error(login_resp.text)
            if err_text:
                # Используем уже готовый multilingual classifier (включая rate-limit!)
                kind, _ = _classify_fault("403", err_text)
                # Map XML-RPC kind → Admin kind
                if kind.value == "rate_limited":
                    return LoginOutcome(
                        error=AdminLoginKind.RATE_LIMITED,
                        error_message=err_text,
                    )
                if kind.value == "auth_invalid":
                    return LoginOutcome(
                        error=AdminLoginKind.AUTH_INVALID,
                        error_message=err_text,
                    )
                if kind.value == "permission_denied":
                    return LoginOutcome(
                        error=AdminLoginKind.PERMISSION_DENIED,
                        error_message=err_text,
                    )
                return LoginOutcome(
                    error=AdminLoginKind.AUTH_INVALID,  # default — login страница вернулась с ошибкой
                    error_message=err_text,
                )
            # 200 без #login_error — проверяем не CF/parking/blocked-page ли это.
            # JS anti-bot интерстициал ("Verifying that you are not a robot") →
            # CF_CHALLENGE → posting flow запустит Tier 3 (FlareSolverr).
            if _is_cf_challenge(login_resp.text, login_resp.status_code):
                return LoginOutcome(
                    error=AdminLoginKind.CF_CHALLENGE,
                    error_message="JS anti-bot challenge на login (нужен Tier 3 / FlareSolverr)",
                )
            from infrastructure.wp_client.client import _classify_html_response, _looks_parked
            if _looks_parked(str(login_resp.url), login_resp.text):
                return LoginOutcome(
                    error=AdminLoginKind.PARKED,
                    error_message=f"{site.domain} parked / suspended after POST",
                )
            if _is_blocked_page(login_resp.text):
                return LoginOutcome(
                    error=AdminLoginKind.RATE_LIMITED,
                    error_message="Security plugin block after POST",
                )
            # Прогон через общий HTML-классификатор — поймает CF challenge и т.п.
            xkind, xmsg = _classify_html_response(200, login_resp.text or "")
            if xkind.value != "broken_endpoint":
                return LoginOutcome(
                    error=_xmlrpc_kind_to_admin_kind(xkind.value),
                    error_message=xmsg,
                )
            # Реально странный 200 — JS-redirect / 2FA / кастомный flow
            return LoginOutcome(
                error=AdminLoginKind.UNKNOWN,
                error_message="200 ответ без #login_error — JS-redirect / 2FA / кастомный login flow",
            )

        # 4xx/5xx после POST — используем HTML-классификатор
        if login_resp.status_code >= 400:
            from infrastructure.wp_client.client import _classify_html_response
            xkind, xmsg = _classify_html_response(login_resp.status_code, login_resp.text or "")
            return LoginOutcome(
                error=_xmlrpc_kind_to_admin_kind(xkind.value),
                error_message=xmsg or f"HTTP {login_resp.status_code} после POST",
            )

        return LoginOutcome(
            error=AdminLoginKind.UNKNOWN,
            error_message=f"unexpected status {login_resp.status_code}",
        )

    async def _verify_admin_access(self, base: str) -> tuple[bool, str | None]:
        """GET /wp-admin/ — проверяем что попали в админку и не вышло
        что нас редиректнули обратно на /wp-login.php.

        Возвращает (ok, error_message)."""
        try:
            resp = await self._client.get(
                f"{base}/wp-admin/", timeout=self._timeout, follow_redirects=True
            )
        except Exception as e:
            return (False, f"GET /wp-admin/ failed: {e}")

        if "wp-login.php" in str(resp.url):
            return (False, "WP редиректнул на /wp-login.php (cookies не приняты)")
        if resp.status_code != 200:
            return (False, f"HTTP {resp.status_code} на /wp-admin/")
        # Маркеры что попали в админку
        lower = resp.text[:8192].lower()
        if 'id="wpadminbar"' in lower or 'id="adminmenu"' in lower or "dashboard" in lower:
            return (True, None)
        return (False, "no admin markers in /wp-admin/ response")

    @staticmethod
    def _parse_login_error(html_text: str) -> str | None:
        """Извлекаем текст из <div id="login_error"> на login странице."""
        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return None
        nodes = tree.xpath('//*[@id="login_error"]')
        if not nodes:
            return None
        text = nodes[0].text_content().strip()
        # Уберём префикс «Error: »/«Ошибка: »/итд — WP его иногда добавляет
        for prefix in ("Error: ", "ERROR: ", "Ошибка: ", "Erreur : ", "Fehler: "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text[:500] or None

    # ─── Capability probes ────────────────────────────────────────

    async def probe_capabilities(self, site: "WpSite") -> AdminCapabilities:
        """
        После успешного login — собрать набор capability флагов.
        Каждый probe = один GET без post-mutations.
        """
        base = self._site_base_url(site)
        caps = AdminCapabilities()

        # 1. /wp-admin/post-new.php → есть ли #title и форма submit
        caps.can_post_via_admin = await self._probe_url_for_marker(
            f"{base}/wp-admin/post-new.php", 'id="title"',
        )
        # 2. /wp-admin/edit.php?post_type=page → есть ли список pages
        caps.can_edit_pages = await self._probe_url_for_marker(
            f"{base}/wp-admin/edit.php?post_type=page", 'id="the-list"',
        )
        # 3. /wp-admin/widgets.php
        caps.can_edit_widgets = await self._probe_url_for_marker(
            f"{base}/wp-admin/widgets.php", "widgets-right",
        )
        # 4. /wp-admin/theme-editor.php → есть редактор + НЕТ «File editing is disabled»
        theme_ok, theme_disabled = await self._probe_theme_editor(base)
        caps.can_edit_themes = theme_ok
        caps.file_editing_disabled = theme_disabled

        # 5. /wp-admin/profile.php → admin role (грубо: ищем role-* класс на body)
        caps.admin_role = await self._probe_admin_role(base)

        # 6. Homepage probe — wp_version + show_on_front
        await self._probe_homepage(base, caps)

        return caps

    async def _probe_url_for_marker(self, url: str, marker: str) -> bool | None:
        """GET url, return True/False if marker found / not in response."""
        try:
            resp = await self._client.get(url, timeout=self._timeout, follow_redirects=True)
        except Exception:
            return None
        if resp.status_code != 200:
            return False
        return marker.lower() in resp.text[:32768].lower()

    async def _probe_theme_editor(self, base: str) -> tuple[bool | None, bool | None]:
        """Возвращает (can_edit_themes, file_editing_disabled)."""
        try:
            resp = await self._client.get(
                f"{base}/wp-admin/theme-editor.php",
                timeout=self._timeout,
                follow_redirects=True,
            )
        except Exception:
            return (None, None)
        if resp.status_code != 200:
            return (False, None)
        lower = resp.text[:32768].lower()
        # Если есть «File editing is disabled» — отдельно фиксируем + can_edit=False
        for m in _FILE_EDIT_DISABLED_MARKERS:
            if m in lower:
                return (False, True)
        # Есть редактор?
        if 'id="newcontent"' in lower or 'class="codepress' in lower or '<textarea name="newcontent"' in lower:
            return (True, False)
        return (False, None)

    @staticmethod
    def _role_from_admin_menu(html: str) -> str | None:
        """Определяем роль по составу левого админ-меню (#adminmenu).

        Меню рендерится строго по capabilities текущего юзера, работает на
        одних cookies (без nonce/REST) и доступно на любой wp-admin странице,
        включая profile.php (есть у всех ролей). Лесенка от старшей роли:

          administrator → Settings / Plugins / Users  (manage_options и т.п.)
          editor        → Pages / Comments            (edit_others_posts)
          author        → Media (upload.php)           (upload_files)
          contributor   → Posts (edit.php)             (edit_posts)
          subscriber    → только Dashboard / Profile
        """
        lower = html.lower()
        # Изолируем блок #adminmenu, чтобы ссылки из контента не давали ложных срабатываний
        m = re.search(r'id="adminmenu".*?(?=id="wpfooter"|id="wpcontent"|</body>)', lower, re.S)
        block = m.group(0) if m else lower
        has = lambda *keys: any(k in block for k in keys)  # noqa: E731
        if has("options-general.php", "plugins.php", 'href="users.php"',
               "href='users.php'", "themes.php"):
            return "administrator"
        # editor — ТОЛЬКО по Pages (edit_pages). Comments (edit-comments.php) сюда
        # НЕ годится: меню Comments рендерится при edit_posts, т.е. его видят и
        # author, и contributor — иначе author ложно классифицируется как editor.
        if has("edit.php?post_type=page"):
            return "editor"
        if has("upload.php"):          # Media → upload_files → author (не contributor)
            return "author"
        if has("edit.php"):            # Posts → edit_posts → contributor
            return "contributor"
        # вошли в админку, но меню пустое → как минимум subscriber
        if 'id="adminmenu"' in lower:
            return "subscriber"
        return None

    async def _probe_admin_role(self, base: str) -> str | None:
        """Роль из admin-страниц. Основной сигнал — состав #adminmenu (cookie-only,
        надёжно, рендерится на ЛЮБОЙ wp-admin странице). Пробуем несколько страниц:
        profile.php → index.php (dashboard) → wp-admin/, т.к. security-плагины часто
        блокируют именно profile.php (403), но дашборд при этом открыт.
        Fallback — старый поиск body-class role-*."""
        for path in ("/wp-admin/profile.php", "/wp-admin/index.php", "/wp-admin/"):
            try:
                resp = await self._client.get(
                    f"{base}{path}", timeout=self._timeout, follow_redirects=True,
                )
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            # НЕ обрезаем агрессивно: на «тяжёлых» сайтах плагины генерят 200–600 KB
            # инлайн-CSS/JS в <head> ДО левого #adminmenu — обрезка резала меню на
            # середине (виден только Posts → ложный contributor) или целиком (None).
            # Потолок 3 MB — защита от патологий, реальные wp-admin страницы меньше.
            html = resp.text[:3_000_000]
            role = self._role_from_admin_menu(html)
            if role:
                return role
            # fallback: body class "user-edit role-administrator" (при редактировании юзера)
            lower = html[:32768].lower()
            for r in ("administrator", "editor", "author", "contributor", "subscriber"):
                if f"role-{r}" in lower:
                    return r
        return None

    async def _get_rest_nonce(self, base: str) -> str | None:
        """REST-nonce для cookie-auth. admin-ajax (валидируем что это nonce, а
        не HTML), fallback — скрейп wpApiSettings со страницы wp-admin."""
        # 1. admin-ajax?action=rest-nonce
        try:
            r = await self._client.get(
                f"{base}/wp-admin/admin-ajax.php?action=rest-nonce",
                timeout=self._timeout, follow_redirects=True,
            )
            cand = (r.text or "").strip()
            if r.status_code == 200 and re.fullmatch(r"[a-zA-Z0-9]{8,16}", cand):
                return cand
        except Exception:
            pass
        # 2. скрейп wpApiSettings из wp-admin
        try:
            r = await self._client.get(
                f"{base}/wp-admin/", timeout=self._timeout, follow_redirects=True,
            )
            return self._extract_rest_nonce(r.text or "")
        except Exception:
            return None

    async def fetch_role_and_caps(self, site: "WpSite") -> tuple[str | None, dict]:
        """Роль + capabilities. Должен вызываться после успешного login().

        Стратегия (от надёжного к точному):
          1. profile.php body-class → роль (работает на одних cookies, без nonce)
          2. REST users/me?context=edit + X-WP-Nonce → точные caps (best-effort)
          3. если REST не дал caps — выводим create_users из роли
            (по умолчанию только administrator имеет create_users)

        На любой сбой возвращаем что смогли (роль из шага 1, caps из роли).
        """
        base = self._site_base_url(site)
        role = await self._probe_admin_role(base)   # cookie-only, надёжно
        caps: dict = {}

        # REST для точных caps — best-effort
        nonce = await self._get_rest_nonce(base)
        if nonce:
            try:
                resp = await self._client.get(
                    f"{base}/wp-json/wp/v2/users/me?context=edit",
                    headers={"X-WP-Nonce": nonce},
                    timeout=self._timeout, follow_redirects=True,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    roles = data.get("roles") or []
                    # REST users/me авторитетнее эвристики по меню — если ответил,
                    # его роль перебивает menu-сигнал (исключаем рассинхрон вида
                    # «menu=contributor, но REST=administrator»).
                    if roles:
                        for r in ("administrator", "editor", "author",
                                  "contributor", "subscriber"):
                            if r in roles:
                                role = r
                                break
                        else:
                            role = str(roles[0])
                    raw = data.get("capabilities") or {}
                    if isinstance(raw, dict):
                        caps = {k: bool(v) for k, v in raw.items()}
            except Exception as e:
                log.debug("admin.role_rest.error", error=str(e))

        # Вывод create_users из роли, если REST не дал
        if "create_users" not in caps and role:
            caps["create_users"] = (role == "administrator")
        return (role, caps)

    async def _probe_homepage(self, base: str, caps: AdminCapabilities) -> None:
        """Парсим <meta name="generator">, <meta property="og:..."> с homepage."""
        try:
            resp = await self._client.get(base + "/", timeout=self._timeout, follow_redirects=True)
        except Exception:
            return
        if resp.status_code != 200:
            return
        try:
            tree = lxml_html.fromstring(resp.text)
        except Exception:
            return
        # WP version
        gen = tree.xpath('//meta[@name="generator"]/@content')
        if gen:
            content = gen[0]
            if "wordpress" in content.lower():
                # «WordPress 6.4.3» → "6.4.3"
                parts = content.split()
                if len(parts) >= 2:
                    caps.wp_version = parts[1][:32]
        # Active theme — из stylesheet links
        css = tree.xpath('//link[@rel="stylesheet"]/@href')
        for href in css:
            if "/wp-content/themes/" in href:
                # https://x.com/wp-content/themes/twentytwentythree/style.css
                try:
                    theme = href.split("/wp-content/themes/")[1].split("/")[0]
                    caps.active_theme = theme[:120]
                    break
                except Exception:
                    pass

    # ─── Create post via admin ────────────────────────────────────

    @staticmethod
    def _extract_rest_nonce(html_text: str) -> str | None:
        """REST-nonce (`X-WP-Nonce`) из inline-JS страницы редактора.

        Gutenberg встраивает `wpApiSettings = {"root":...,"nonce":"abc"}` и/или
        `wp.apiFetch` middleware. Берём nonce оттуда.
        """
        # wpApiSettings = {... "nonce":"<hex>" ...}
        m = re.search(
            r'wpApiSettings\s*=\s*\{[^}]*?"nonce"\s*:\s*"([a-f0-9]+)"',
            html_text, re.IGNORECASE,
        )
        if m:
            return m.group(1)
        # generic: createNonceMiddleware('abc') / "X-WP-Nonce":"abc"
        m = re.search(r'createNonceMiddleware\(\s*"([a-f0-9]+)"', html_text)
        if m:
            return m.group(1)
        m = re.search(r'"X-WP-Nonce"\s*:\s*"([a-f0-9]+)"', html_text)
        if m:
            return m.group(1)
        return None

    async def _create_post_via_rest(
        self, base: str, page_html: str, title: str, content: str, status: str,
    ) -> "PostViaAdminOutcome | None":
        """Gutenberg fallback: POST /wp-json/wp/v2/posts с REST-nonce.

        Возвращает PostViaAdminOutcome или None (если REST недоступен — caller
        отдаст диагностику). Использует session-cookies (уже залогинены).
        """
        nonce = self._extract_rest_nonce(page_html)
        if not nonce:
            # Надёжный fallback: встроенный admin-ajax action отдаёт свежий
            # REST-nonce plain-text-ом (работает даже когда страница не
            # инлайнит wpApiSettings — частый случай в lazy Gutenberg shell).
            try:
                nr = await self._client.get(
                    f"{base}/wp-admin/admin-ajax.php?action=rest-nonce",
                    timeout=self._timeout, follow_redirects=True,
                )
                cand = (nr.text or "").strip()
                if nr.status_code == 200 and re.fullmatch(r"[a-zA-Z0-9]{8,16}", cand):
                    nonce = cand
            except Exception:
                pass
        if not nonce:
            return None
        rest_url = f"{base}/wp-json/wp/v2/posts"
        payload = {
            "title": title or "",
            "content": content,
            "status": status,  # publish | draft | private
        }
        try:
            resp = await self._client.post(
                rest_url,
                json=payload,
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                        error_message=f"REST POST: {e}")

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
                pid = data.get("id")
                link = data.get("link") or (f"{base}/?p={pid}" if pid else None)
                return PostViaAdminOutcome(
                    error=AdminPostKind.OK, post_id=pid, posted_url=link,
                )
            except Exception:
                return PostViaAdminOutcome(error=AdminPostKind.OK)
        if resp.status_code in (401, 403):
            # nonce/права не прошли через REST
            return PostViaAdminOutcome(
                error=AdminPostKind.NO_PERMISSION,
                error_message=f"REST {resp.status_code}: {resp.text[:150]}",
            )
        if resp.status_code == 404:
            # REST API выключен/закрыт — не наш кейс, отдаём None для диагностики
            return None
        return PostViaAdminOutcome(
            error=AdminPostKind.UNKNOWN,
            error_message=f"REST status {resp.status_code}: {resp.text[:120]}",
        )

    async def create_post(
        self,
        site: "WpSite",
        title: str,
        content: str,
        status: str = "publish",
    ) -> PostViaAdminOutcome:
        """
        Создать пост через wp-admin (НЕ XML-RPC). Должен быть вызван после
        успешного `login()`.

        Шаги:
            1. GET /wp-admin/post-new.php — собираем nonce + post_id (WP создаёт
               draft autoдля каждого open new-post запроса)
            2. POST /wp-admin/post.php с action=editpost
            3. Парсим redirect / response

        Известные ограничения:
            - Block Editor (Gutenberg) использует REST API + JSON под капотом —
              для него нужен другой flow. Сейчас работает с классическим editor
              (большинство WP всё ещё).
        """
        base = self._site_base_url(site)
        new_post_url = f"{base}/wp-admin/post-new.php"

        try:
            new_resp = await self._client.get(
                new_post_url, timeout=self._timeout, follow_redirects=True
            )
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                        error_message=f"GET new-post: {e}")

        # Если редиректнуло на login — cookies протухли
        if "wp-login.php" in str(new_resp.url):
            return PostViaAdminOutcome(error=AdminPostKind.NOT_LOGGED_IN,
                                        error_message="cookies expired")
        if new_resp.status_code == 403:
            return PostViaAdminOutcome(error=AdminPostKind.NO_PERMISSION,
                                        error_message="403 на post-new.php")

        # Парсим скрытые поля
        try:
            tree = lxml_html.fromstring(new_resp.text)
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                        error_message=f"bad html: {e}")

        # post_ID — auto-draft id
        post_id_node = tree.xpath('//input[@id="post_ID"]/@value')
        # _wpnonce — главный nonce для form-submit
        nonce_node = tree.xpath('//input[@id="_wpnonce"]/@value')
        # _wp_http_referer — обычно требуется
        referer_node = tree.xpath('//input[@name="_wp_http_referer"]/@value')
        # user_ID — WP это требует
        user_id_node = tree.xpath('//input[@id="user_ID"]/@value')

        if not nonce_node or not post_id_node:
            # Classic Editor полей нет → Gutenberg (block editor). Fallback на
            # REST API /wp-json/wp/v2/posts с REST-nonce из страницы.
            rest_outcome = await self._create_post_via_rest(
                base, new_resp.text, title, content, status,
            )
            if rest_outcome is not None:
                return rest_outcome
            return PostViaAdminOutcome(
                error=AdminPostKind.UNKNOWN,
                error_message="Gutenberg: не нашли REST-nonce для /wp-json fallback",
            )

        post_id = post_id_node[0]
        nonce = nonce_node[0]
        referer = referer_node[0] if referer_node else "/wp-admin/post-new.php"
        user_id = user_id_node[0] if user_id_node else ""

        # POST на /wp-admin/post.php
        post_url = f"{base}/wp-admin/post.php"
        form_data = {
            "_wpnonce": nonce,
            "_wp_http_referer": referer,
            "user_ID": user_id,
            "action": "editpost",
            "originalaction": "editpost",
            "post_author": user_id,
            "post_type": "post",
            "original_post_status": "auto-draft",
            "post_ID": post_id,
            "post_title": title or "",
            "content": content,
            "post_status": status,  # publish | draft | private | trash
            "visibility": "public",
            "publish": "Publish",
        }
        try:
            post_resp = await self._client.post(
                post_url,
                data=form_data,
                timeout=self._timeout,
                follow_redirects=False,
                headers={"Referer": f"{base}{referer}"},
            )
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                        error_message=f"POST post.php: {e}")

        if post_resp.status_code in (301, 302):
            loc = post_resp.headers.get("Location", "")
            # WP редиректит на /wp-admin/post.php?post=ID&action=edit&message=6
            # message=6 = опубликовано, message=1 = обновлено, message=10 = сохранено
            if "post=" in loc and "action=edit" in loc:
                # Извлекаем post_id из location
                try:
                    pid_str = loc.split("post=")[1].split("&")[0]
                    pid = int(pid_str)
                except (ValueError, IndexError):
                    pid = None
                # Construct permalink
                permalink = f"{base}/?p={pid}" if pid else None
                return PostViaAdminOutcome(
                    error=AdminPostKind.OK,
                    post_id=pid,
                    posted_url=permalink,
                )
            # Иначе редирект странный
            return PostViaAdminOutcome(
                error=AdminPostKind.UNKNOWN,
                error_message=f"unexpected redirect: {loc[:120]}",
            )

        if post_resp.status_code == 200:
            # Скорее всего «Are you sure you want to do this?» — nonce expired
            if "are you sure" in post_resp.text.lower() or "nonce" in post_resp.text.lower():
                return PostViaAdminOutcome(error=AdminPostKind.NONCE_FAIL,
                                            error_message="nonce verification failed")
        return PostViaAdminOutcome(
            error=AdminPostKind.UNKNOWN,
            error_message=f"unexpected status {post_resp.status_code}",
        )

    # ─── Provision: создание нового пользователя ─────────────────────

    async def _create_user_via_rest(
        self, base: str, username: str, email: str, password: str, role: str,
    ) -> "UserCreateOutcome | None":
        """POST /wp-json/wp/v2/users (cookie + X-WP-Nonce). Чистый путь — НЕ шлёт
        e-mail уведомлений. Возвращает:
          - OK / DUPLICATE — определённый ответ REST;
          - None — REST недоступен/закрыт (404, не-JSON 200, 401/403 nonce) →
            caller должен попробовать форму user-new.php.
        """
        nonce = await self._get_rest_nonce(base)
        if not nonce:
            return None
        try:
            resp = await self._client.post(
                f"{base}/wp-json/wp/v2/users",
                json={"username": username, "email": email, "password": password,
                      "roles": [role]},
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                timeout=self._timeout, follow_redirects=False,
            )
        except Exception:
            return None
        ctype = resp.headers.get("content-type", "")
        # REST «выключен» security-плагином часто = 200 с HTML. Не-JSON → fallback.
        if "json" not in ctype:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if resp.status_code in (200, 201) and isinstance(data, dict) and data.get("id"):
            return UserCreateOutcome(
                error=AdminUserCreateKind.OK, user_id=int(data["id"]),
                username=username, role=role, via="rest",
            )
        code = data.get("code") if isinstance(data, dict) else None
        if code in ("existing_user_login", "existing_user_email"):
            return UserCreateOutcome(
                error=AdminUserCreateKind.DUPLICATE, username=username, via="rest",
                error_message=str(data.get("message", code))[:200],
            )
        # 401/403/нонс/прочее — пусть попробует форму
        return None

    async def create_user(
        self, site: "WpSite", username: str, email: str, password: str,
        role: str = "author",
    ) -> UserCreateOutcome:
        """Создать нового WP-пользователя. Вызывать ПОСЛЕ успешного login()
        админ-кредом с правом create_users.

        Стратегия: сначала REST (/wp/v2/users — не шлёт письма), при недоступности
        REST — форма /wp-admin/user-new.php (работает везде, где работает admin).
        """
        base = self._site_base_url(site)

        # 1. REST — приоритет (чисто, без e-mail уведомлений)
        rest = await self._create_user_via_rest(base, username, email, password, role)
        if rest is not None:
            return rest

        # 2. Форма wp-admin/user-new.php
        try:
            resp = await self._client.get(
                f"{base}/wp-admin/user-new.php", timeout=self._timeout,
                follow_redirects=True,
            )
        except Exception as e:
            return UserCreateOutcome(error=AdminUserCreateKind.UNKNOWN,
                                     error_message=f"GET user-new.php: {e}")
        if "wp-login.php" in str(resp.url):
            return UserCreateOutcome(error=AdminUserCreateKind.NOT_LOGGED_IN,
                                     error_message="cookies expired")
        if resp.status_code == 403:
            return UserCreateOutcome(error=AdminUserCreateKind.NO_PERMISSION,
                                     error_message="403 на user-new.php")
        try:
            tree = lxml_html.fromstring(resp.text)
        except Exception as e:
            return UserCreateOutcome(error=AdminUserCreateKind.UNKNOWN,
                                     error_message=f"bad html: {e}")
        nonce_node = (tree.xpath('//input[@id="_wpnonce_create-user"]/@value')
                      or tree.xpath('//input[@name="_wpnonce_create-user"]/@value'))
        referer_node = tree.xpath('//input[@name="_wp_http_referer"]/@value')
        if not nonce_node:
            # ни REST, ни форма — нет права/закрыто
            return UserCreateOutcome(
                error=AdminUserCreateKind.NO_PERMISSION,
                error_message="не нашли nonce формы создания пользователя",
            )
        referer = referer_node[0] if referer_node else "/wp-admin/user-new.php"
        form = {
            "action": "createuser",
            "_wpnonce_create-user": nonce_node[0],
            "_wp_http_referer": referer,
            "user_login": username,
            "email": email,
            "first_name": "", "last_name": "", "url": "",
            "pass1": password, "pass1-text": password, "pass2": password,
            "pw_weak": "on",   # на случай если хостинг считает пароль слабым
            "role": role,
            # send_user_notification НЕ передаём → письмо новому юзеру не уходит
            "createuser": "Add New User",
        }
        try:
            post_resp = await self._client.post(
                f"{base}/wp-admin/user-new.php", data=form,
                timeout=self._timeout, follow_redirects=False,
                headers={"Referer": f"{base}/wp-admin/user-new.php"},
            )
        except Exception as e:
            return UserCreateOutcome(error=AdminUserCreateKind.UNKNOWN,
                                     error_message=f"POST user-new.php: {e}")

        if post_resp.status_code in (301, 302):
            loc = post_resp.headers.get("Location", "")
            if "update=add" in loc:
                uid = None
                if "user_id=" in loc:
                    try:
                        uid = int(loc.split("user_id=")[1].split("&")[0])
                    except (ValueError, IndexError):
                        uid = None
                return UserCreateOutcome(
                    error=AdminUserCreateKind.OK, user_id=uid,
                    username=username, role=role, via="form",
                )
            return UserCreateOutcome(error=AdminUserCreateKind.UNKNOWN,
                                     error_message=f"unexpected redirect: {loc[:160]}")

        # 200 — форма перерисована с ошибкой
        body = (post_resp.text or "").lower()
        if "already registered" in body or "already in use" in body or "уже существ" in body:
            return UserCreateOutcome(error=AdminUserCreateKind.DUPLICATE, username=username,
                                     via="form", error_message="username/email уже существует")
        if "are you sure" in body or "security check" in body:
            return UserCreateOutcome(error=AdminUserCreateKind.NONCE_FAIL, via="form",
                                     error_message="nonce verification failed")
        return UserCreateOutcome(
            error=AdminUserCreateKind.UNKNOWN, via="form",
            error_message=f"unexpected status {post_resp.status_code}",
        )


    # ─── Link placement (sitewide / homepage) ───────────────────────

    async def _rest_get(self, base: str, path: str, nonce: str):
        """GET REST JSON с X-WP-Nonce. Возвращает (status, json|None)."""
        try:
            r = await self._client.get(
                f"{base}/wp-json{path}", headers={"X-WP-Nonce": nonce},
                timeout=self._timeout, follow_redirects=True,
            )
        except Exception:
            return (0, None)
        if "json" not in r.headers.get("content-type", ""):
            return (r.status_code, None)
        try:
            return (r.status_code, r.json())
        except Exception:
            return (r.status_code, None)

    async def probe_link_placement(
        self, site: "WpSite", nonce: str | None = None,
    ) -> LinkPlacementProbe:
        """JIT-снимок сайта: тип темы (FSE?), footer-sidebar, footer-меню,
        footer template-part, show_on_front. Best-effort — нет данных → None-поля."""
        base = self._site_base_url(site)
        nonce = nonce or await self._get_rest_nonce(base)
        p = LinkPlacementProbe()
        if not nonce:
            p.error = "no_nonce"
            return p

        # settings (для homepage-типа)
        _, settings = await self._rest_get(base, "/wp/v2/settings", nonce)
        if isinstance(settings, dict):
            p.show_on_front = settings.get("show_on_front")
            p.page_on_front = settings.get("page_on_front")

        # активная тема — block (FSE)?
        _, themes = await self._rest_get(base, "/wp/v2/themes?status=active", nonce)
        if isinstance(themes, list) and themes:
            p.is_block_theme = bool(themes[0].get("is_block_theme"))

        # sidebars → footer (исключаем wp_inactive_widgets — холдинг-зона, не рендерится)
        _, sidebars = await self._rest_get(base, "/wp/v2/sidebars", nonce)
        if isinstance(sidebars, list):
            real = [s for s in sidebars
                    if "inactive" not in str(s.get("id", "")).lower()]
            p.sidebars = [{"id": s.get("id"), "name": s.get("name")} for s in real]
            # только настоящие footer-области (сквозной рендер). Нет footer → None
            footer = next((s for s in real
                           if "footer" in str(s.get("id", "")).lower()
                           or "footer" in str(s.get("name", "")).lower()), None)
            if footer:
                p.footer_sidebar_id = footer.get("id")

        # меню + локации. Формат: {loc: {"name","menu": <id>, ...}} — берём .menu (>0).
        _, menus = await self._rest_get(base, "/wp/v2/menus", nonce)
        if isinstance(menus, list):
            p.nav_menus = [{"id": m.get("id"), "name": m.get("name")} for m in menus]
        _, locations = await self._rest_get(base, "/wp/v2/menu-locations", nonce)
        if isinstance(locations, dict) and locations:
            def _loc_menu(loc: str) -> int:
                v = locations.get(loc)
                mid = v.get("menu") if isinstance(v, dict) else v
                try:
                    return int(mid)
                except (TypeError, ValueError):
                    return 0
            # приоритет локаций: footer > header > primary > main > любая с menu>0
            def _rank(loc: str) -> int:
                ll = loc.lower()
                for i, kw in enumerate(("footer", "header", "primary", "main")):
                    if kw in ll:
                        return i
                return 9
            for loc in sorted(locations.keys(), key=_rank):
                mid = _loc_menu(loc)
                if mid > 0:
                    p.footer_menu_id = mid
                    break

        # FSE footer template-part
        if p.is_block_theme:
            _, parts = await self._rest_get(base, "/wp/v2/template-parts", nonce)
            if isinstance(parts, list):
                fp = next((tp for tp in parts if str(tp.get("area", "")).lower() == "footer"), None)
                if fp:
                    p.footer_template_part_id = fp.get("id")
        return p

    @staticmethod
    def _link_html(url: str, anchor: str) -> str:
        return f'<a href="{escape(url, quote=True)}">{escape(anchor)}</a>'

    async def _place_via_widget(self, base, nonce, probe, url, anchor) -> str | None:
        sidebar = probe.footer_sidebar_id
        if not sidebar:
            return None
        try:
            r = await self._client.post(
                f"{base}/wp-json/wp/v2/widgets",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"id_base": "custom_html", "sidebar": sidebar,
                      "instance": {"raw": {"title": "", "content": self._link_html(url, anchor)}}},
                timeout=self._timeout, follow_redirects=False,
            )
            if r.status_code in (200, 201):
                wid = r.json().get("id")
                return str(wid) if wid else None
        except Exception as e:
            log.debug("link.widget.error", error=str(e))
        return None

    async def _place_via_nav_menu(self, base, nonce, probe, url, anchor) -> str | None:
        if not probe.footer_menu_id:
            return None
        try:
            r = await self._client.post(
                f"{base}/wp-json/wp/v2/menu-items",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"title": anchor, "url": url, "status": "publish",
                      "menus": probe.footer_menu_id},
                timeout=self._timeout, follow_redirects=False,
            )
            if r.status_code in (200, 201):
                mid = r.json().get("id")
                return str(mid) if mid else None
        except Exception as e:
            log.debug("link.navmenu.error", error=str(e))
        return None

    async def _place_via_fse(self, base, nonce, probe, url, anchor) -> str | None:
        part_id = probe.footer_template_part_id
        if not part_id:
            return None
        status, part = await self._rest_get(base, f"/wp/v2/template-parts/{part_id}", nonce)
        if not isinstance(part, dict):
            return None
        content = part.get("content")
        raw = content.get("raw") if isinstance(content, dict) else (content or "")
        block = (f'<!-- wp:html --><a href="{escape(url, quote=True)}" '
                 f'data-glref="{part_id}">{escape(anchor)}</a><!-- /wp:html -->')
        try:
            r = await self._client.post(   # POST = update в WP REST
                f"{base}/wp-json/wp/v2/template-parts/{part_id}",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"content": (raw or "") + block},
                timeout=self._timeout, follow_redirects=False,
            )
            if r.status_code in (200, 201):
                return f"fse:{part_id}"
        except Exception as e:
            log.debug("link.fse.error", error=str(e))
        return None

    async def place_sitewide_link(
        self, site: "WpSite", url: str, anchor: str, *,
        probe: LinkPlacementProbe | None = None,
    ) -> LinkPlaceOutcome:
        """Поставить сквозную ссылку. Цепочка REST-методов с обязательным verify:
        каждый метод размещает → verify (анонимно) → не подтвердилось → откат и
        следующий метод. Вызывать после успешного login() администратором."""
        base = self._site_base_url(site)
        nonce = await self._get_rest_nonce(base)
        if not nonce:
            return LinkPlaceOutcome(error=LinkPlaceKind.NO_NONCE)
        probe = probe or await self.probe_link_placement(site, nonce=nonce)

        # FSE-тему пробуем шаблоном первым; иначе виджет → меню
        chain: list[tuple[str, object]] = []
        if probe.is_block_theme:
            chain.append(("fse_template", self._place_via_fse))
        chain += [("widget", self._place_via_widget), ("nav_menu", self._place_via_nav_menu)]

        for via, method in chain:
            ref = await method(base, nonce, probe, url, anchor)
            if not ref:
                continue
            ok, urls = await self.verify_link(site, url)
            if ok:
                return LinkPlaceOutcome(error=LinkPlaceKind.OK, placed_via=via,
                                        placement_ref=ref, verified_urls=urls)
            # разместили, но verify не прошёл → откат, пробуем следующий
            await self.remove_sitewide_link(site, via, ref, nonce=nonce)
        return LinkPlaceOutcome(error=LinkPlaceKind.NO_METHOD,
                                error_message="ни один метод не дал verified-ссылку")

    # ─── Homepage link (ссылка с главной) ────────────────────────────

    async def _homepage_template_id(self, base: str, nonce: str) -> str | None:
        """ID шаблона главной для блочной темы: front-page → home → index."""
        _, tpls = await self._rest_get(base, "/wp/v2/templates", nonce)
        if not isinstance(tpls, list):
            return None
        by_slug = {t.get("slug"): t.get("id") for t in tpls if t.get("id")}
        for slug in ("front-page", "home", "index"):
            if by_slug.get(slug):
                return by_slug[slug]
        return None

    async def _place_home_via_page(self, base, nonce, page_id, url, anchor) -> str | None:
        """Дописать ссылку в контент статической главной (page_on_front)."""
        _, page = await self._rest_get(base, f"/wp/v2/pages/{page_id}?context=edit", nonce)
        if not isinstance(page, dict):
            return None
        content = page.get("content")
        raw = content.get("raw") if isinstance(content, dict) else (content or "")
        block = (f'<!-- wp:html --><p><a href="{escape(url, quote=True)}" '
                 f'data-glref="home{page_id}">{escape(anchor)}</a></p><!-- /wp:html -->')
        try:
            r = await self._client.post(
                f"{base}/wp-json/wp/v2/pages/{page_id}",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"content": (raw or "") + block},
                timeout=self._timeout, follow_redirects=False)
            if r.status_code in (200, 201):
                return f"page:{page_id}"
        except Exception as e:
            log.debug("link.home_page.error", error=str(e))
        return None

    async def _place_home_via_fse(self, base, nonce, url, anchor) -> str | None:
        """Вставить блок в FSE-шаблон главной (front-page/home)."""
        tpl_id = await self._homepage_template_id(base, nonce)
        if not tpl_id:
            return None
        _, tpl = await self._rest_get(base, f"/wp/v2/templates/{tpl_id}", nonce)
        if not isinstance(tpl, dict):
            return None
        content = tpl.get("content")
        raw = content.get("raw") if isinstance(content, dict) else (content or "")
        block = (f'<!-- wp:html --><a href="{escape(url, quote=True)}" '
                 f'data-glref="htpl">{escape(anchor)}</a><!-- /wp:html -->')
        try:
            r = await self._client.post(
                f"{base}/wp-json/wp/v2/templates/{tpl_id}",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"content": (raw or "") + block},
                timeout=self._timeout, follow_redirects=False)
            if r.status_code in (200, 201):
                return f"htpl:{tpl_id}"
        except Exception as e:
            log.debug("link.home_fse.error", error=str(e))
        return None

    async def place_homepage_link(
        self, site: "WpSite", url: str, anchor: str, *,
        probe: LinkPlacementProbe | None = None,
    ) -> LinkPlaceOutcome:
        """Ссылка с главной. Блочная тема → правим FSE-шаблон главной; статическая
        главная (show_on_front=page) → дописываем в контент страницы. С verify."""
        base = self._site_base_url(site)
        nonce = await self._get_rest_nonce(base)
        if not nonce:
            return LinkPlaceOutcome(error=LinkPlaceKind.NO_NONCE)
        probe = probe or await self.probe_link_placement(site, nonce=nonce)

        chain: list[tuple[str, object]] = []
        if probe.is_block_theme:
            chain.append(("home_template",
                          lambda: self._place_home_via_fse(base, nonce, url, anchor)))
        if probe.show_on_front == "page" and probe.page_on_front:
            chain.append(("home_page",
                          lambda: self._place_home_via_page(base, nonce, probe.page_on_front, url, anchor)))

        if not chain:
            return LinkPlaceOutcome(
                error=LinkPlaceKind.NO_METHOD,
                error_message="главная — лента постов на классической теме (нет REST-метода)")

        for via, method in chain:
            ref = await method()
            if not ref:
                continue
            ok, urls = await self.verify_link(site, url)
            if ok:
                return LinkPlaceOutcome(error=LinkPlaceKind.OK, placed_via=via,
                                        placement_ref=ref, verified_urls=urls)
            await self.remove_sitewide_link(site, via, ref, nonce=nonce)
        return LinkPlaceOutcome(error=LinkPlaceKind.NO_METHOD,
                                error_message="homepage: ни один метод не дал verified-ссылку")

    async def verify_link(
        self, site: "WpSite", url: str,
    ) -> tuple[bool, list[str]]:
        """Анонимно (без наших cookies) проверяем, что ссылка реально на странице.
        Тянем главную + 1 внутреннюю. Возвращаем (видна_ли_на_главной, urls)."""
        base = self._site_base_url(site)
        found: list[str] = []
        home_ok = False
        # cache-buster: query-string заставляет page-cache плагины (WP Super Cache/
        # W3TC) отдать свежий рендер, а не закэшированную версию. Без него verify
        # ловит старую страницу (ложный успех при удалении / ложный провал при
        # размещении). uuid вместо time/random — стабильно и уникально.
        cb = uuid.uuid4().hex[:12]

        def _bust(u: str) -> str:
            return u + ("&" if "?" in u else "?") + f"_glcb={cb}"

        async def _anon_get(u: str) -> str:
            """Гостевой GET (без cookies). httpx → при CF/WAF-блоке curl_cffi
            (Chrome TLS) тем же proxy: иначе verify на CF-сайтах всегда ложно-провал."""
            bu = _bust(u)
            status, text = 0, ""
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=True, verify=False,
                    headers={"User-Agent": self._client.headers.get("User-Agent", "Mozilla/5.0")},
                ) as anon:
                    r = await anon.get(bu)
                    status, text = r.status_code, r.text
            except Exception as e:
                log.debug("link.verify.error", error=str(e))
            from infrastructure.cf_transport import cf_fetch, looks_cf_blocked
            if (not text) or looks_cf_blocked(status, text):
                alt = await cf_fetch(bu, proxy=self._proxy_url, timeout=self._timeout)
                if alt is not None and not looks_cf_blocked(alt[0], alt[1]):
                    text = alt[1]
            return text

        # видим как гость/поисковик (без наших cookies)
        home = await _anon_get(base + "/")
        if self._html_has_link(home, url):
            home_ok = True
            found.append(base + "/")
        # одна внутренняя страница — первая внутренняя ссылка с главной
        inner = self._first_internal_url(home, base)
        if inner:
            body2 = await _anon_get(inner)
            if self._html_has_link(body2, url):
                found.append(inner)
        # verified = видна хотя бы на главной (сквозная → на каждой странице)
        return (home_ok, found)

    @staticmethod
    def _html_has_link(html: str, url: str) -> bool:
        if not html:
            return False
        h = html.lower()
        u = url.lower().strip()
        variants = {u, u.rstrip("/"), u.replace("https://", "").replace("http://", ""),
                    u.replace("&", "&amp;")}
        return any(v and v in h for v in variants)

    @staticmethod
    def _first_internal_url(html: str, base: str) -> str | None:
        from urllib.parse import urlparse
        host = urlparse(base).netloc
        for m in re.finditer(r"""href=["']([^"']+)["']""", html or ""):
            href = m.group(1)
            if href.startswith("#") or "wp-login" in href or "/wp-admin" in href:
                continue
            if href.startswith("/") and not href.startswith("//"):
                return base + href
            if host and host in href and urlparse(href).path not in ("", "/"):
                return href
        return None

    async def _strip_block(self, base: str, nonce: str, rest_path: str, marker: str) -> bool:
        """Вырезать наш блок (по data-glref=marker) из content REST-объекта
        (template-part / template / page) и сохранить обратно."""
        clean_path = rest_path.split("?")[0]
        # context=edit обязателен — иначе content.raw отсутствует (только rendered)
        _, obj = await self._rest_get(base, f"{clean_path}?context=edit", nonce)
        if not isinstance(obj, dict):
            return False
        content = obj.get("content")
        raw = content.get("raw") if isinstance(content, dict) else (content or "")
        if not raw:
            return False
        # снимаем <a data-glref="marker">...</a> вместе с опциональными wp:html/<p> обёртками
        cleaned = re.sub(
            r'(?:<!-- wp:html -->\s*)?(?:<p>\s*)?<a [^>]*data-glref="'
            + re.escape(marker) + r'"[^>]*>.*?</a>(?:\s*</p>)?(?:\s*<!-- /wp:html -->)?',
            "", raw, flags=re.S)
        try:
            r = await self._client.post(
                f"{base}/wp-json{rest_path.split('?')[0]}",
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                json={"content": cleaned}, timeout=self._timeout)
            return r.status_code in (200, 201)
        except Exception as e:
            log.debug("link.strip_block.error", path=rest_path, error=str(e))
            return False

    async def remove_sitewide_link(
        self, site: "WpSite", placed_via: str, placement_ref: str,
        *, nonce: str | None = None,
    ) -> bool:
        """Удалить ранее размещённую сквозную ссылку по placed_via+ref."""
        base = self._site_base_url(site)
        nonce = nonce or await self._get_rest_nonce(base)
        if not nonce:
            return False
        try:
            if placed_via == "widget":
                r = await self._client.delete(
                    f"{base}/wp-json/wp/v2/widgets/{placement_ref}?force=true",
                    headers={"X-WP-Nonce": nonce}, timeout=self._timeout)
                return r.status_code in (200, 204)
            if placed_via == "nav_menu":
                r = await self._client.delete(
                    f"{base}/wp-json/wp/v2/menu-items/{placement_ref}?force=true",
                    headers={"X-WP-Nonce": nonce}, timeout=self._timeout)
                return r.status_code in (200, 204)
            if placed_via == "fse_template" and placement_ref.startswith("fse:"):
                part_id = placement_ref[4:]
                return await self._strip_block(
                    base, nonce, f"/wp/v2/template-parts/{part_id}", part_id)
            if placed_via == "home_template" and placement_ref.startswith("htpl:"):
                tpl_id = placement_ref[5:]
                return await self._strip_block(
                    base, nonce, f"/wp/v2/templates/{tpl_id}", "htpl")
            if placed_via == "home_page" and placement_ref.startswith("page:"):
                page_id = placement_ref[5:]
                return await self._strip_block(
                    base, nonce, f"/wp/v2/pages/{page_id}", f"home{page_id}")
        except Exception as e:
            log.debug("link.remove.error", via=placed_via, error=str(e))
        return False


    # ─── Update / delete опубликованного поста (admin REST) ──────────

    async def update_post_via_rest(
        self, site: "WpSite", post_id: int, title: str, content: str,
    ) -> PostViaAdminOutcome:
        """Перезалить контент в пост через REST (нужна admin-сессия + nonce)."""
        base = self._site_base_url(site)
        nonce = await self._get_rest_nonce(base)
        if not nonce:
            return PostViaAdminOutcome(error=AdminPostKind.NOT_LOGGED_IN,
                                       error_message="no REST nonce")
        try:
            resp = await self._client.post(
                f"{base}/wp-json/wp/v2/posts/{post_id}",
                json={"title": title or "", "content": content},
                headers={"X-WP-Nonce": nonce, "Content-Type": "application/json"},
                timeout=self._timeout, follow_redirects=False)
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN, error_message=str(e))
        if resp.status_code in (200, 201):
            try:
                d = resp.json()
                return PostViaAdminOutcome(error=AdminPostKind.OK, post_id=post_id,
                                           posted_url=d.get("link"))
            except Exception:
                return PostViaAdminOutcome(error=AdminPostKind.OK, post_id=post_id)
        if resp.status_code in (401, 403):
            return PostViaAdminOutcome(error=AdminPostKind.NO_PERMISSION,
                                       error_message=f"REST {resp.status_code}")
        return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                   error_message=f"REST {resp.status_code}: {resp.text[:120]}")

    async def delete_post_via_rest(
        self, site: "WpSite", post_id: int,
    ) -> PostViaAdminOutcome:
        """Удалить пост через REST (force=true → в корзину минуя trash)."""
        base = self._site_base_url(site)
        nonce = await self._get_rest_nonce(base)
        if not nonce:
            return PostViaAdminOutcome(error=AdminPostKind.NOT_LOGGED_IN,
                                       error_message="no REST nonce")
        try:
            resp = await self._client.delete(
                f"{base}/wp-json/wp/v2/posts/{post_id}?force=true",
                headers={"X-WP-Nonce": nonce}, timeout=self._timeout)
        except Exception as e:
            return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN, error_message=str(e))
        if resp.status_code in (200, 201):
            return PostViaAdminOutcome(error=AdminPostKind.OK, post_id=post_id)
        if resp.status_code in (401, 403):
            return PostViaAdminOutcome(error=AdminPostKind.NO_PERMISSION,
                                       error_message=f"REST {resp.status_code}")
        return PostViaAdminOutcome(error=AdminPostKind.UNKNOWN,
                                   error_message=f"REST {resp.status_code}")


# ─── High-level wrapper для posting worker-а ─────────────────────────


# Маппинг AdminPostKind/AdminLoginKind → wp_client.ErrorKind, чтобы posting
# worker мог переиспользовать unified обработку (capability marking, site
# failure tracking, retry policy).
def _admin_login_kind_to_post_outcome(login_kind: AdminLoginKind, msg: str | None):
    """Когда login упал — возвращаем PostOutcome-совместимый объект чтобы
    основной flow в Celery worker мог принять решение."""
    from infrastructure.wp_client import ErrorKind, PostOutcome

    mapping = {
        AdminLoginKind.AUTH_INVALID: ErrorKind.AUTH_INVALID,
        AdminLoginKind.PERMISSION_DENIED: ErrorKind.PERMISSION_DENIED,
        AdminLoginKind.CF_CHALLENGE: ErrorKind.CF_CHALLENGE,
        AdminLoginKind.RATE_LIMITED: ErrorKind.RATE_LIMITED,
        AdminLoginKind.LOGIN_DISABLED: ErrorKind.XMLRPC_DISABLED,  # wp-login.php тоже мёртв
        AdminLoginKind.SITE_NOT_FOUND: ErrorKind.SITE_NOT_FOUND,
        AdminLoginKind.NETWORK: ErrorKind.NETWORK,
        AdminLoginKind.SERVER_ERROR: ErrorKind.SERVER_ERROR,
        AdminLoginKind.PARKED: ErrorKind.PARKED,
        AdminLoginKind.UNKNOWN: ErrorKind.UNKNOWN,
    }
    return PostOutcome(error=mapping.get(login_kind, ErrorKind.UNKNOWN), error_message=msg)


def _admin_post_kind_to_post_outcome(
    post_kind: AdminPostKind, msg: str | None,
    post_id: int | None = None, posted_url: str | None = None,
):
    """Конвертация AdminPostKind в PostOutcome."""
    from infrastructure.wp_client import ErrorKind, PostOutcome

    if post_kind == AdminPostKind.OK:
        return PostOutcome(
            error=ErrorKind.OK, post_id=post_id, posted_url=posted_url,
        )
    mapping = {
        AdminPostKind.NO_PERMISSION: ErrorKind.PERMISSION_DENIED,
        AdminPostKind.NONCE_FAIL: ErrorKind.UNKNOWN,  # ретраить редко помогает — nonce expired
        AdminPostKind.NOT_LOGGED_IN: ErrorKind.AUTH_INVALID,  # cookies протухли
        AdminPostKind.UNKNOWN: ErrorKind.UNKNOWN,
    }
    return PostOutcome(error=mapping.get(post_kind, ErrorKind.UNKNOWN), error_message=msg)


async def post_via_admin(
    http: httpx.AsyncClient,
    *,
    site: "WpSite",
    login: str,
    password: str,
    title: str,
    content: str,
    timeout_seconds: int = 30,
    proxy_url: str | None = None,
):
    """
    Полный Tier 2 post flow:
       1. login() — form-login через /wp-login.php
       2. при успехе → create_post() с собранными session cookies
       3. возврат PostOutcome (совместимо с XmlRpcPoster.post())

    Используется как fallback из Celery posting worker-а на XMLRPC_DISABLED.
    """
    client = WpAdminClient(http, timeout_seconds=timeout_seconds, proxy_url=proxy_url)
    login_res = await client.login(site=site, login=login, password=password)
    if not login_res.success:
        return _admin_login_kind_to_post_outcome(login_res.error, login_res.error_message)

    post_res = await client.create_post(site=site, title=title, content=content, status="publish")
    return _admin_post_kind_to_post_outcome(
        post_res.error, post_res.error_message,
        post_id=post_res.post_id, posted_url=post_res.posted_url,
    )
