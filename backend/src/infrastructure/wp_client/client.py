"""
XML-RPC клиент с lazy discovery + cached canonical URL.

Поток:
1. Если `WpSite.last_working_url` есть → POST туда напрямую.
2. Иначе строим candidates (https/http × www/no-www × hint_path/hint_port)
   и пробуем GET /xmlrpc.php — ищем маркер "XML-RPC server accepts POST requests only".
3. Первый успех → POST туда + сохраняем URL в БД (caller обновляет site).
4. Самолечение: при 404/auth ошибках вызывающий сбрасывает last_working_url
   → следующий пост запустит discovery заново.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from html import escape
from typing import TYPE_CHECKING

import httpx
import structlog
from lxml import etree

if TYPE_CHECKING:
    from infrastructure.db.models import WpSite

log = structlog.get_logger(__name__)


# ─── Result types ────────────────────────────────────────────────────


class ErrorKind(StrEnum):
    """Категория ошибки — определяет что делать дальше."""
    OK = "ok"
    # Ошибки credential — попробовать другой login на этом же сайте
    AUTH_INVALID = "auth_invalid"
    PERMISSION_DENIED = "permission_denied"
    # Ошибки сайта — переходим к следующему сайту
    SITE_NOT_FOUND = "site_not_found"          # 404, домен не отвечает
    XMLRPC_DISABLED = "xmlrpc_disabled"        # endpoint не найден
    PARKED = "parked"                           # parking page / suspended / cPanel default
    NETWORK = "network"                         # timeout / connection error
    SERVER_ERROR = "server_error"               # 5xx
    TASK_TIMEOUT = "task_timeout"              # весь validation выбил жёсткий потолок
    RATE_LIMITED = "rate_limited"               # HTTP 429 от плагина / хостинга
    CF_CHALLENGE = "cf_challenge"              # Cloudflare/Sucuri/Wordfence challenge HTML
    BROKEN_ENDPOINT = "broken_endpoint"        # xmlrpc.php существует, но отдаёт мусор (пусто/HTML/HTML 5xx)
    CAPTCHA_REQUIRED = "captcha_required"      # Сайт требует решить CAPTCHA/reCAPTCHA — без браузера никак
    # Прочее
    UNKNOWN = "unknown"


# Определённые («definitive») ошибки credential: пароль/права не «починятся» сами.
# ЕДИНЫЙ источник правды и для валидатора, и для постинга — встретив такую ошибку,
# оба помечают cred невалидным (порог 1). Так пул работающих доступов держится
# чистым одинаково на обоих путях.
DEFINITIVE_CRED_INVALID_KINDS: frozenset[ErrorKind] = frozenset(
    {ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED}
)


# Параркинговые / suspended / cPanel-default страницы. Если discovery упёрся
# в такое — сайт мёртв на этом домене, нет смысла перебирать остальные
# https/http × www кандидаты.
_PARKING_URL_MARKERS = (
    "/cgi-sys/",                # cPanel error landing (default Apache)
    "/cgi-sys/suspendedpage",   # явный suspended
    "/sitebuilder/",            # некоторые хостеры
)
_PARKING_BODY_MARKERS = (
    "this account has been suspended",
    "account suspended",
    "this domain has been registered",   # registrar parking
    "this domain is for sale",
    "domain for sale",
    "buy this domain",
    "default web site page",      # IIS default
    "it works!",                   # apache default
    "welcome to nginx!",           # nginx default
    "index of /",                  # Apache directory listing — нет WP в корне
)


def _looks_parked(final_url: str, body_text: str) -> bool:
    """True если ответ — это parking / suspended / default web page, а не WP."""
    url_lower = (final_url or "").lower()
    for m in _PARKING_URL_MARKERS:
        if m in url_lower:
            return True
    # body может быть огромный — режем
    body = (body_text or "")[:8192].lower()
    for m in _PARKING_BODY_MARKERS:
        if m in body:
            return True
    return False


@dataclass
class PostOutcome:
    error: ErrorKind
    post_id: int | None = None
    posted_url: str | None = None
    working_xmlrpc_url: str | None = None  # обновить site.last_working_url
    error_message: str | None = None
    posted_via: str | None = None  # 'xmlrpc' | 'admin' — какой канал реально сработал

    @property
    def success(self) -> bool:
        return self.error == ErrorKind.OK


@dataclass
class ValidateOutcome:
    """Результат `wp.getUsersBlogs` validation-call-а — без побочек."""
    error: ErrorKind
    working_xmlrpc_url: str | None = None
    error_message: str | None = None
    # WP-роль из wp.getProfile (снимаем при успешной auth). None — не определили.
    role: str | None = None
    # Каким каналом подтверждён cred. None — XML-RPC (Tier 1, дефолт).
    # "admin_browser" — прошли CF + залогинились браузером (Patchright) на
    # CF-сайте: валиден через admin-канал, XML-RPC НЕ доказан. Тогда
    # _apply_validation_result ставит can_admin_login (не can_xmlrpc) и
    # помечает сайт cf_protected.
    valid_via: str | None = None

    @property
    def success(self) -> bool:
        return self.error == ErrorKind.OK


# ─── XML payload builders ────────────────────────────────────────────


_NEW_POST_TEMPLATE = """<?xml version="1.0"?>
<methodCall>
  <methodName>wp.newPost</methodName>
  <params>
    <param><value><int>1</int></value></param>
    <param><value><string>{login}</string></value></param>
    <param><value><string>{password}</string></value></param>
    <param>
      <value>
        <struct>
          <member>
            <name>post_status</name>
            <value><string>publish</string></value>
          </member>
          <member>
            <name>post_title</name>
            <value><string>{title}</string></value>
          </member>
          <member>
            <name>post_content</name>
            <value><string>{content}</string></value>
          </member>
          <member>
            <name>post_date</name>
            <value><dateTime.iso8601>{post_date}</dateTime.iso8601></value>
          </member>
        </struct>
      </value>
    </param>
  </params>
</methodCall>"""


# Лёгкая ручка для валидации credential — wp.getUsersBlogs не создаёт постов
# и возвращает массив доступных юзеру блогов. Если auth корректен — успех.
_GET_USERS_BLOGS_TEMPLATE = """<?xml version="1.0"?>
<methodCall>
  <methodName>wp.getUsersBlogs</methodName>
  <params>
    <param><value><string>{login}</string></value></param>
    <param><value><string>{password}</string></value></param>
  </params>
</methodCall>"""


def _build_new_post_xml(login: str, password: str, title: str, content: str, post_date: datetime) -> bytes:
    body = _NEW_POST_TEMPLATE.format(
        login=escape(login),
        password=escape(password),
        title=escape(title or ""),
        content=escape(content),
        post_date=post_date.strftime("%Y%m%dT%H:%M:%S"),  # WP iso8601 format без TZ
    )
    return body.encode("utf-8")


def _build_get_users_blogs_xml(login: str, password: str) -> bytes:
    body = _GET_USERS_BLOGS_TEMPLATE.format(login=escape(login), password=escape(password))
    return body.encode("utf-8")


# wp.editPost / wp.deletePost — править/удалять уже опубликованный пост.
_EDIT_POST_TEMPLATE = """<?xml version="1.0"?>
<methodCall>
  <methodName>wp.editPost</methodName>
  <params>
    <param><value><int>1</int></value></param>
    <param><value><string>{login}</string></value></param>
    <param><value><string>{password}</string></value></param>
    <param><value><int>{post_id}</int></value></param>
    <param><value><struct>
      <member><name>post_title</name><value><string>{title}</string></value></member>
      <member><name>post_content</name><value><string>{content}</string></value></member>
      {extra_members}
    </struct></value></param>
  </params>
</methodCall>"""

_DELETE_POST_TEMPLATE = """<?xml version="1.0"?>
<methodCall>
  <methodName>wp.deletePost</methodName>
  <params>
    <param><value><int>1</int></value></param>
    <param><value><string>{login}</string></value></param>
    <param><value><string>{password}</string></value></param>
    <param><value><int>{post_id}</int></value></param>
  </params>
</methodCall>"""


def _build_edit_post_xml(login, password, post_id, title, content, slug=None) -> bytes:
    # slug → post_name (меняет permalink). Пустой slug — не трогаем URL поста.
    extra = (f"<member><name>post_name</name><value><string>{escape(slug)}</string>"
             f"</value></member>") if slug else ""
    return _EDIT_POST_TEMPLATE.format(
        login=escape(login), password=escape(password), post_id=int(post_id),
        title=escape(title or ""), content=escape(content),
        extra_members=extra).encode("utf-8")


def _build_delete_post_xml(login, password, post_id) -> bytes:
    return _DELETE_POST_TEMPLATE.format(
        login=escape(login), password=escape(password), post_id=int(post_id)).encode("utf-8")


def _parse_simple_response(xml_text: str) -> tuple["ErrorKind", str | None]:
    """Разбор ответа editPost/deletePost: <boolean>1</boolean> = ok, иначе fault."""
    try:
        tree = etree.fromstring(
            xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except etree.XMLSyntaxError:
        return _classify_html_response(200, xml_text or "")
    if tree.xpath("//fault"):
        code = (tree.xpath("//fault//member[name='faultCode']/value/int/text()") or ["?"])[0]
        msg = (tree.xpath("//fault//member[name='faultString']/value/string/text()") or ["fault"])[0]
        return _classify_fault(code, msg)
    if tree.xpath("//params/param/value"):
        return (ErrorKind.OK, None)
    return (ErrorKind.UNKNOWN, "unexpected response shape")


# wp.getProfile(blog_id, username, password, fields) — отдаёт WP-роль текущего
# юзера напрямую (поле `roles`), по логину+паролю, без nonce и без захода в
# wp-admin. Работает для ЛЮБОЙ роли (administrator/editor/author/contributor/
# subscriber). blog_id=1 — стандарт; на не-multisite игнорируется.
_GET_PROFILE_TEMPLATE = """<?xml version="1.0"?>
<methodCall>
  <methodName>wp.getProfile</methodName>
  <params>
    <param><value><int>1</int></value></param>
    <param><value><string>{login}</string></value></param>
    <param><value><string>{password}</string></value></param>
    <param><value><array><data><value><string>roles</string></value></data></array></value></param>
  </params>
</methodCall>"""

# Приоритет ролей: при наличии нескольких берём «старшую».
_ROLE_PRIORITY = ("administrator", "editor", "author", "contributor", "subscriber")


def _build_get_profile_xml(login: str, password: str) -> bytes:
    body = _GET_PROFILE_TEMPLATE.format(login=escape(login), password=escape(password))
    return body.encode("utf-8")


def _parse_profile_role(xml_text: str) -> str | None:
    """Достаём WP-роль из ответа wp.getProfile. None при fault/пустом/мусоре."""
    try:
        tree = etree.fromstring(
            xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        )
    except etree.XMLSyntaxError:
        return None
    if tree.xpath("//fault"):
        return None
    roles = [
        r.strip().lower()
        for r in tree.xpath("//member[name='roles']/value/array/data/value/string/text()")
        if r and r.strip()
    ]
    if not roles:
        return None
    for r in _ROLE_PRIORITY:
        if r in roles:
            return r
    return roles[0]


# ─── XML response parsing ────────────────────────────────────────────


_XMLRPC_MARKER = "XML-RPC server accepts POST requests only"


# ─── Multi-language WP fault classification ─────────────────────────
#
# Стратегия:
#   1. NUMERIC faultCode — основной сигнал. WP всегда возвращает 403 для
#      неверного логина, 401 для permission, 405 для disabled XML-RPC
#      ВНЕ зависимости от языка. Это спецификация WP.
#   2. Текстовые паттерны — fallback на случай если плагин/security-mod
#      переопределил коды. Сюда — переводы стандартных сообщений WP на
#      основные локали (ES/DE/FR/IT/PT/RU/PL/NL/TR/JA/ZH/ID) и
#      ключевые слова security-плагинов (Wordfence, Limit Login Attempts).
#
# Паттерны — все в lowercase. Сверяем через `in lower(msg)`.

_AUTH_INVALID_PATTERNS = (
    # English (WP core + variants from real prod data)
    "incorrect username or password",
    "incorrect password",
    "bad username/password",
    "username or password you entered is incorrect",
    "password you entered for the username",
    "password you entered for the email",
    "invalid login details",
    "invalid username",
    "invalid password",
    "incorrect email address or password",
    "entered credentials are wrong",
    "not registered on this site",         # «The username X is not registered on this site» = wrong username
    "unknown email address",
    "verification required",                # 2FA-style cred fail
    "insecure password",                     # «INSECURE PASSWORD: Your login attempt has been blocked» — Wordfence по password-leak listам
    "cookies are blocked",                   # WP requires cookies — наша httpx сессия cookie не приняла
    "datos de acceso no válidos",            # ES: «Invalid login details»
    "autenticación fallida",                  # ES: «Authentication failed»
    "adresse e-mail inconnue",                # FR: email unknown
    "endereço de e-mail desconhecido",       # PT-BR: email unknown
    "endereço de e-mail",                     # PT-BR (general email error)
    "dirección de correo electrónico desconocida",  # ES: email unknown
    "nome de usuário ou a senha que você digitou estão incorretos",  # PT-BR full variant
    "el nombre de usuario o la contraseña que ingresaste son incorrectos",  # ES full variant
    "la contraseña que has introducido",     # ES «the password you entered…»
    "no es correcta",                         # ES «is not correct»
    # Spanish
    # Spanish
    "nombre de usuario o contraseña incorrecto",
    "contraseña incorrecta",
    # German
    "falscher benutzername oder passwort",
    "benutzername oder passwort falsch",
    "falsches passwort",
    # French
    "identifiant ou mot de passe incorrect",
    "mot de passe incorrect",
    # Italian
    "nome utente o password errati",
    "nome utente o password non corrette",  # spotted in prod (otticapergolesistore.com)
    "password errata",
    "password non corretta",
    # Macedonian (spotted in prod: insulinskaporta.mk)
    "неточно корисничко име",
    "неточно корисничкото име",
    "погрешна лозинка",
    # Turkish (real prod variants)
    "yazdığınız kullanıcı adı veya parola yanlış",
    "kullanıcı adı veya şifre yanlış",
    "parola geçersiz",
    "yanlış parola",
    # Polish full variant (prod)
    "błąd: nieprawidłowa nazwa użytkownika lub hasło",
    "nieprawidłowa nazwa użytkownika lub hasło",
    # Generic «invalid credentials» в нескольких языках
    "invalid username or incorrect password",
    "wrong credentials",
    # Portuguese (BR + PT)
    "nome de usuário ou senha incorretos",
    "senha incorreta",
    "utilizador ou palavra-passe incorret",
    # Russian
    "неверное имя пользователя",
    "неверный пароль",
    "неправильный логин или пароль",
    # Polish
    "nieprawidłowa nazwa użytkownika",
    "nieprawidłowe hasło",
    # Dutch
    "onjuiste gebruikersnaam",
    "ongeldig wachtwoord",
    # Turkish
    "kullanıcı adı veya şifre",
    "yanlış şifre",
    # Indonesian
    "nama pengguna atau kata sandi",
    "kata sandi salah",
    # Japanese (rough — kanji кодировка)
    "ユーザー名またはパスワード",
    "パスワードが間違っています",
    # Simplified Chinese
    "用户名或密码不正确",
    "密码错误",
)

_PERMISSION_DENIED_PATTERNS = (
    # English
    "permission",
    "sorry, you are not allowed",
    "sorry, you cannot",
    "not authorized",
    # Spanish
    "no tienes permiso",
    "no estás autorizado",
    # German
    "keine berechtigung",
    "nicht erlaubt",
    # French
    "vous n'avez pas",
    "non autorisé",
    "autorisation",
    # Italian
    "non hai i permessi",
    "non autorizzato",
    # Portuguese
    "sem permissão",
    "não autorizado",
    # Russian
    "недостаточно прав",
    "нет прав",
    "не авторизованы",
)

_XMLRPC_DISABLED_PATTERNS = (
    # English (WP / plugins)
    "xml-rpc services are disabled",
    "xml-rpc is disabled",
    "xmlrpc disabled",
    "xml-rpc not enabled",
    # Common security plugins disable message
    "xmlrpc has been disabled",
)

# Rate-limit / IP-block от security-плагинов (Wordfence, Limit Login Attempts,
# iThemes Security и т.п.). Это **не** «неверный пароль» — это «попробуй
# попозже». Мы пока маппим в PERMISSION_DENIED чтобы пометить cred как
# проблемный, но при желании можно добавить новый kind RATE_LIMITED.
_RATE_LIMITED_PATTERNS = (
    "you have been blocked",
    "you've been blocked",
    "too many failed login",
    "too many login attempts",
    "attempts remaining",
    "your access to this site has been limited",
    "wordfence",
    "limit login attempts",
    "please try again later",
    "temporarily locked",
    "ip has been blocked",
    # Real prod variants (Limit Login Attempts / iThemes Security)
    "attempt(s) left",          # «2 attempt(s) left», «1 attempt(s) left»
    " attempts left",
    "lockout",
    "locked out",
    "you have been locked",
)

# CAPTCHA / reCAPTCHA / hCaptcha — на сайте стоит проверка-капча.
# Без реального браузера решить нельзя (требует JS + интеракцию).
# Помечаем cred как `captcha_required` — она по сути жива, но через httpx
# попасть нельзя. Для постинга нужен Tier 3 (FlareSolverr/Playwright).
_CAPTCHA_REQUIRED_PATTERNS = (
    # English
    "please complete the recaptcha",
    "recaptcha verification failed",
    "incorrect captcha entered",
    "please check the recaptcha",
    "complete the captcha",
    "we need to make sure you're not a robot",
    "verify that you are human",
    "please verify that you are human",
    # Спецификация Wordfence / Login No Captcha reCAPTCHA / Loginizer и т.п.
    "your answer was incorrect",      # «security question» fail
    # Spanish (real prod)
    "verificación de recaptcha ha fallado",
    "por favor, comprueba la caja de recaptcha",
    "comprueba la caja de recaptcha",
    # Chinese (real prod)
    "请输入正确的验证码",                # «Please enter the correct verification code»
    "验证码错误",                        # «captcha error»
    # Generic
    "captcha",                          # last-resort catch-all (с учётом что мы фильтруем рано)
)


# Маркеры CF/Sucuri/Wordfence challenge страниц (если XML-RPC endpoint
# отдаёт HTML вместо XML — это обычно security plugin или CDN block).
_CF_HTML_MARKERS = (
    "checking your browser before accessing",
    "just a moment...",
    "/cdn-cgi/challenge-platform/",
    "cloudflare ray id",
    "cf-error-details",
    "data-cf-beacon",
    "challenges.cloudflare.com",
    "sucuri website firewall",
    "your access to this site has been limited",
    "wordfence",
    # JS anti-bot интерстициалы (часто HTTP 200) — нужен Tier 3 (FlareSolverr)
    "verifying that you are not a robot",
    "verifying you are not a robot",
    "verifying that you are human",
    "verifying you are human",
    "verify you are human",
    "please wait while we verify",
    "ddos protection by",
    "enable javascript and cookies to continue",
)


def _classify_html_response(
    status: int, body: str, expected_xml: bool = True
) -> tuple[ErrorKind, str | None]:
    """
    Когда XML-RPC endpoint должен был отдать XML, но отдал что-то другое.

    Раньше всё это валилось в UNKNOWN. Теперь различаем:
      - 429 → RATE_LIMITED (плагины Limit-Login и т.п.)
      - CF/Sucuri/Wordfence HTML → CF_CHALLENGE
      - parking page → PARKED (используем существующий детектор)
      - HTML 5xx → SERVER_ERROR
      - пустое тело → BROKEN_ENDPOINT
      - всё остальное → UNKNOWN
    """
    body_snippet = (body or "")[:8192]

    # Проверки сначала по статусу, потом по содержимому
    if status == 429:
        return (ErrorKind.RATE_LIMITED, f"http 429 {body_snippet[:120].strip()!r}")
    if status >= 500:
        return (ErrorKind.SERVER_ERROR, f"http {status} (HTML body)")

    if not body_snippet.strip():
        return (ErrorKind.BROKEN_ENDPOINT, "empty body")

    lower = body_snippet.lower()
    # CF/security plugin challenge
    for m in _CF_HTML_MARKERS:
        if m in lower:
            return (ErrorKind.CF_CHALLENGE, f"security challenge: {m}")
    # Parking / suspended (reuse существующий детектор)
    # Берём URL пустой здесь — _looks_parked сам справится по body markers
    if _looks_parked("", body_snippet):
        return (ErrorKind.PARKED, "parking / suspended page")

    # Если это HTML — endpoint живой, но что-то странное вернул
    if "<html" in lower or "<!doctype" in lower:
        # Извлечём <title> если есть — для error_message
        try:
            tree = etree.fromstring(
                body_snippet.encode("utf-8"), parser=etree.HTMLParser()
            )
            title_nodes = tree.xpath("//title/text()") if tree is not None else []
            title = title_nodes[0].strip() if title_nodes else "unknown HTML"
        except Exception:
            title = "unknown HTML"
        return (
            ErrorKind.BROKEN_ENDPOINT,
            f"HTML response (title: {title[:100]})",
        )

    # Не XML, не HTML — что-то совсем странное
    return (ErrorKind.BROKEN_ENDPOINT, f"non-XML body: {body_snippet[:100]!r}")


def _classify_fault(code: str, msg: str) -> tuple[ErrorKind, str | None]:
    """
    Классифицировать XML-RPC fault response.
    Возвращает (kind, error_message_for_db).

    Порядок проверок:
      1. Rate-limit паттерны — ПЕРЕД numeric code. Иначе 403 + «Too many failed
         login attempts» (типичный Limit-Login-Attempts plugin) спутался бы с
         настоящим auth-fail и cred ушла бы в is_valid=False, хотя пароль может
         быть рабочий — нас просто залочили на время.
      2. NUMERIC codes — спецификация WP, не зависит от языка.
      3. Multilingual text patterns — для нестандартных fault-codes от плагинов.
    """
    lower = (msg or "").lower()
    err_msg = f"{code}: {msg}"

    # 1. Rate-limit (ПЕРЕД numeric code — см. docstring выше)
    for p in _RATE_LIMITED_PATTERNS:
        if p in lower:
            return (ErrorKind.RATE_LIMITED, err_msg)

    # 2. CAPTCHA — тоже до numeric code. На сайте стоит капча, без браузера
    # пройти не можем. Cred сама по себе может быть рабочая.
    for p in _CAPTCHA_REQUIRED_PATTERNS:
        if p in lower:
            return (ErrorKind.CAPTCHA_REQUIRED, err_msg)

    # 3. NUMERIC codes — спецификация WP, не зависит от языка
    if code == "403":
        return (ErrorKind.AUTH_INVALID, err_msg)
    if code == "401":
        return (ErrorKind.PERMISSION_DENIED, err_msg)
    if code == "405":
        return (ErrorKind.XMLRPC_DISABLED, err_msg)

    # 4. AUTH patterns (multilingual)
    for p in _AUTH_INVALID_PATTERNS:
        if p in lower:
            return (ErrorKind.AUTH_INVALID, err_msg)

    # 4. Permission patterns (multilingual)
    for p in _PERMISSION_DENIED_PATTERNS:
        if p in lower:
            return (ErrorKind.PERMISSION_DENIED, err_msg)

    # 5. XML-RPC disabled patterns
    for p in _XMLRPC_DISABLED_PATTERNS:
        if p in lower:
            return (ErrorKind.XMLRPC_DISABLED, err_msg)

    return (ErrorKind.UNKNOWN, err_msg)


def _parse_post_response(
    xml_text: str, status: int = 200
) -> tuple[ErrorKind, int | None, str | None]:
    """
    Распарсить XML-RPC ответ wp.newPost.
    Возвращает (error_kind, post_id, error_message).

    Если ответ — не XML (пустой / HTML / мусор), пытаемся классифицировать
    через `_classify_html_response` — спасает кейсы 429 / CF / parking
    которые раньше валились в UNKNOWN.
    """
    try:
        tree = etree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except etree.XMLSyntaxError:
        kind, msg = _classify_html_response(status, xml_text or "")
        return (kind, None, msg)

    # Fault?
    faults = tree.xpath("//fault")
    if faults:
        # <fault><value><struct><member><name>faultCode</name><value><int>403</int>...
        code_nodes = tree.xpath("//fault//member[name='faultCode']/value/int/text()")
        string_nodes = tree.xpath("//fault//member[name='faultString']/value/string/text()")
        code = code_nodes[0] if code_nodes else "?"
        msg = string_nodes[0] if string_nodes else "fault"
        kind, err_msg = _classify_fault(code, msg)
        return (kind, None, err_msg)

    # Успех: <params><param><value><string>POST_ID</string>
    post_id_nodes = tree.xpath("//params/param/value/string/text()")
    if not post_id_nodes:
        post_id_nodes = tree.xpath("//params/param/value/int/text()")
    if post_id_nodes:
        try:
            return (ErrorKind.OK, int(post_id_nodes[0]), None)
        except (ValueError, TypeError):
            return (ErrorKind.UNKNOWN, None, f"bad post_id: {post_id_nodes[0]}")

    return (ErrorKind.UNKNOWN, None, "no post_id and no fault in response")


# ─── Discovery: build candidate URLs ─────────────────────────────────


def build_candidate_urls(site: "WpSite") -> list[str]:
    """
    Порядок кандидатов XML-RPC endpoint-а для discovery.

    HTTPS first (большинство), потом HTTP. Hint path/port — добавляются
    как первые кандидаты если заданы (юзер уже знает что WP в /blog).
    """
    domain = site.domain
    path = site.hint_path or ""
    if path and not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    port_suffix = f":{site.hint_port}" if site.hint_port else ""

    candidates: list[str] = []

    # 1. С hints (если есть) — первыми
    if site.hint_path or site.hint_port:
        for scheme in ("https", "http"):
            for prefix in ("", "www."):
                candidates.append(f"{scheme}://{prefix}{domain}{port_suffix}{path}/xmlrpc.php")

    # 2. Стандартные — без hints, без порта, в корне
    for scheme in ("https", "http"):
        for prefix in ("", "www."):
            url = f"{scheme}://{prefix}{domain}/xmlrpc.php"
            if url not in candidates:
                candidates.append(url)

    return candidates


# ─── Main client ─────────────────────────────────────────────────────


class XmlRpcPoster:
    """
    Одна инстанс на воркер. Использует общий httpx.AsyncClient
    (передаётся снаружи — даёт shared connection pool).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout_seconds: int = 30,
        proxy_url: str | None = None,
    ):
        self._client = client
        self._timeout = timeout_seconds
        # Прокси этого httpx-клиента в виде строки — нужен curl_cffi-слою, т.к.
        # cf_fetch создаёт собственную сессию и не наследует proxy httpx-клиента.
        # Резидентский exit IP критичен: CF режет datacenter-IP по репутации даже
        # с правильным TLS-фингерпринтом, поэтому curl_cffi обязан идти тем же
        # proxy что и основной запрос.
        self._proxy_url = proxy_url
        # Кеш discovery в памяти процесса — на случай если БД не обновили
        # (например, упали между success и commit-ом). Только в рамках одного task.
        self._memo_url: dict[int, str] = {}

    async def _check_xmlrpc(self, url: str) -> tuple[str, str | None]:
        """
        GET /xmlrpc.php. Возвращает (kind, final_url):
          kind ∈ {'found', 'parked', 'miss'}
          final_url — куда нас в итоге отредиректило (полезно для post-а)
        """
        try:
            resp = await self._client.get(url, timeout=self._timeout, follow_redirects=True)
        except (httpx.TimeoutException, httpx.NetworkError):
            return ("miss", None)
        except Exception as e:
            log.warning("xmlrpc.check.error", url=url, error=str(e))
            return ("miss", None)

        final_url = str(resp.url) if resp.url else url
        # Parking / suspended / default-pages — даже если 200 OK
        if _looks_parked(final_url, resp.text):
            return ("parked", final_url)
        if _XMLRPC_MARKER in resp.text:
            return ("found", final_url)
        # Не нашли маркер: возможно CF/WAF блок (403/503/markers). Слой curl_cffi
        # (Chrome TLS) — снимает фингерпринт-блоки без браузера, ДО FlareSolverr.
        from infrastructure.cf_transport import cf_fetch, looks_cf_blocked
        if looks_cf_blocked(resp.status_code, resp.text):
            alt = await cf_fetch(url, proxy=self._proxy_url, timeout=self._timeout)
            if alt and _XMLRPC_MARKER in (alt[1] or ""):
                log.info("xmlrpc.discovery.cf_bypass_curlcffi", url=url)
                return ("found", final_url)
        if resp.status_code >= 500:
            return ("miss", None)
        return ("miss", None)

    async def _discover(self, site: "WpSite") -> tuple[str | None, str | None]:
        """
        Найти живой xmlrpc URL, перебрав кандидатов.
        Возвращает (url, fail_kind):
          - (url, None)        — нашли рабочий endpoint
          - (None, 'parked')   — хоть один кандидат показал parking-page;
                                  смысла дальше не пробовать — домен мёртв
          - (None, None)       — не нашли ничего, обычный xmlrpc_disabled
        """
        if site.id in self._memo_url:
            return (self._memo_url[site.id], None)

        any_parked = False
        for url in build_candidate_urls(site):
            kind, final_url = await self._check_xmlrpc(url)
            if kind == "found":
                resolved = final_url or url
                self._memo_url[site.id] = resolved
                log.info("xmlrpc.discovery.found", site_id=site.id, url=resolved)
                return (resolved, None)
            if kind == "parked":
                any_parked = True
                log.info(
                    "xmlrpc.discovery.parked", site_id=site.id, url=url, final=final_url
                )
                # parking-страницы обычно одинаковые на http/https/www — break
                break

        if any_parked:
            return (None, "parked")
        log.info("xmlrpc.discovery.failed", site_id=site.id, domain=site.domain)
        return (None, None)

    async def post(
        self,
        site: "WpSite",
        login: str,
        password: str,
        title: str,
        content: str,
        post_date: datetime,
    ) -> PostOutcome:
        """
        Опубликовать пост. Discovery если нужно. Возвращает PostOutcome.
        """
        # 1. Получить URL endpoint-а
        url = site.last_working_url or self._memo_url.get(site.id)
        if not url:
            url, fail_kind = await self._discover(site)
            if not url:
                if fail_kind == "parked":
                    return PostOutcome(
                        error=ErrorKind.PARKED,
                        error_message=f"{site.domain} is parked / suspended / default-page",
                    )
                return PostOutcome(
                    error=ErrorKind.XMLRPC_DISABLED,
                    error_message=f"no working xmlrpc endpoint for {site.domain}",
                )

        # 2. POST XML-RPC body
        xml_body = _build_new_post_xml(login, password, title, content, post_date)
        try:
            resp = await self._client.post(
                url,
                content=xml_body,
                timeout=self._timeout,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                follow_redirects=True,
            )
        except httpx.TimeoutException:
            return PostOutcome(error=ErrorKind.NETWORK, error_message="timeout")
        except httpx.NetworkError as e:
            return PostOutcome(error=ErrorKind.NETWORK, error_message=str(e))
        except Exception as e:
            log.warning("xmlrpc.post.error", url=url, error=str(e))
            return PostOutcome(error=ErrorKind.UNKNOWN, error_message=str(e))

        if resp.status_code == 404:
            # URL устарел — сбросим кеш и попросим caller-а пересоздать
            self._memo_url.pop(site.id, None)
            return PostOutcome(error=ErrorKind.SITE_NOT_FOUND, error_message="404 on xmlrpc")
        # 4xx (кроме 405) и 5xx — HTML классификация (CF/parking/429/etc.)
        if resp.status_code >= 400 and resp.status_code != 405:
            kind, err_msg = _classify_html_response(resp.status_code, resp.text or "")
            return PostOutcome(error=kind, error_message=err_msg)

        # 3. Parse response
        kind, post_id, err_msg = _parse_post_response(resp.text)
        if kind != ErrorKind.OK:
            return PostOutcome(
                error=kind,
                error_message=err_msg,
                working_xmlrpc_url=url,  # XML-RPC отвечает, проблема в credential/контенте
            )

        # 4. Построить URL опубликованного поста
        # Простейший вариант: {scheme}://{host}/?p={post_id}. WP сам отрезолвит permalink.
        # Можно потом улучшить через GET и парс <link>, но это +1 запрос на каждый.
        from urllib.parse import urlparse

        parsed = urlparse(url)
        permalink = f"{parsed.scheme}://{parsed.netloc}/?p={post_id}"

        return PostOutcome(
            error=ErrorKind.OK,
            post_id=post_id,
            posted_url=permalink,
            working_xmlrpc_url=url,
        )

    async def _edit_or_delete(self, site, login, password, xml_body) -> PostOutcome:
        """Общий путь для editPost/deletePost: discovery + POST + разбор."""
        url = site.last_working_url or self._memo_url.get(site.id)
        if not url:
            url, fail_kind = await self._discover(site)
            if not url:
                return PostOutcome(
                    error=ErrorKind.PARKED if fail_kind == "parked" else ErrorKind.XMLRPC_DISABLED,
                    error_message=f"no working xmlrpc endpoint for {site.domain}")
        try:
            resp = await self._client.post(
                url, content=xml_body, timeout=self._timeout,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                follow_redirects=True)
        except httpx.TimeoutException:
            return PostOutcome(error=ErrorKind.NETWORK, error_message="timeout")
        except httpx.NetworkError as e:
            return PostOutcome(error=ErrorKind.NETWORK, error_message=str(e))
        except Exception as e:
            return PostOutcome(error=ErrorKind.UNKNOWN, error_message=str(e))
        if resp.status_code == 404:
            self._memo_url.pop(site.id, None)
            return PostOutcome(error=ErrorKind.SITE_NOT_FOUND, error_message="404 on xmlrpc")
        if resp.status_code >= 400 and resp.status_code != 405:
            kind, err_msg = _classify_html_response(resp.status_code, resp.text or "")
            return PostOutcome(error=kind, error_message=err_msg)
        kind, msg = _parse_simple_response(resp.text)
        return PostOutcome(error=kind, error_message=msg, working_xmlrpc_url=url)

    async def edit_post(self, site, login, password, post_id, title, content,
                        slug=None) -> PostOutcome:
        """Перезалить контент в существующий пост (wp.editPost). slug → post_name."""
        return await self._edit_or_delete(
            site, login, password,
            _build_edit_post_xml(login, password, post_id, title, content, slug=slug))

    async def delete_post(self, site, login, password, post_id) -> PostOutcome:
        """Удалить пост (wp.deletePost)."""
        return await self._edit_or_delete(
            site, login, password, _build_delete_post_xml(login, password, post_id))

    async def validate(
        self,
        site: "WpSite",
        login: str,
        password: str,
    ) -> ValidateOutcome:
        """
        Проверка credential без создания контента. POST wp.getUsersBlogs.
        Discovery URL если нужно — кеш в last_working_url как и при post.
        """
        url = site.last_working_url or self._memo_url.get(site.id)
        if not url:
            url, fail_kind = await self._discover(site)
            if not url:
                if fail_kind == "parked":
                    return ValidateOutcome(
                        error=ErrorKind.PARKED,
                        error_message=f"{site.domain} is parked / suspended / default-page",
                    )
                return ValidateOutcome(
                    error=ErrorKind.XMLRPC_DISABLED,
                    error_message=f"no working xmlrpc endpoint for {site.domain}",
                )

        xml_body = _build_get_users_blogs_xml(login, password)
        try:
            resp = await self._client.post(
                url,
                content=xml_body,
                timeout=self._timeout,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                follow_redirects=True,
            )
        except httpx.TimeoutException:
            return ValidateOutcome(error=ErrorKind.NETWORK, error_message="timeout")
        except httpx.NetworkError as e:
            return ValidateOutcome(error=ErrorKind.NETWORK, error_message=str(e))
        except Exception as e:
            log.warning("xmlrpc.validate.error", url=url, error=str(e))
            return ValidateOutcome(error=ErrorKind.UNKNOWN, error_message=str(e))

        status, body = resp.status_code, resp.text
        # CF/WAF блок? Ретрай тем же POST через curl_cffi (Chrome TLS) ДО FlareSolverr —
        # снимает TLS-фингерпринт-блоки без браузера. Если прошло — берём его ответ.
        from infrastructure.cf_transport import cf_fetch, looks_cf_blocked
        if looks_cf_blocked(status, body):
            alt = await cf_fetch(
                url, method="POST", content=xml_body,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                proxy=self._proxy_url, timeout=self._timeout)
            if alt is not None and not looks_cf_blocked(alt[0], alt[1]):
                log.info("xmlrpc.validate.cf_bypass_curlcffi", url=url)
                status, body = alt

        if status == 404:
            self._memo_url.pop(site.id, None)
            return ValidateOutcome(error=ErrorKind.SITE_NOT_FOUND, error_message="404 on xmlrpc")
        # 4xx (кроме 405 — Method Not Allowed для GET — нормально для XML-RPC endpoint)
        # и 5xx — пробуем классифицировать body как HTML (CF/parking/rate-limited/etc.)
        if status >= 400 and status != 405:
            kind, err_msg = _classify_html_response(status, body or "")
            return ValidateOutcome(
                error=kind, working_xmlrpc_url=url, error_message=err_msg,
            )

        # Парсим: faultCode 403/401 → AUTH_INVALID, иначе если есть <array> → ok.
        try:
            tree = etree.fromstring(
                body.encode("utf-8") if isinstance(body, str) else body
            )
        except etree.XMLSyntaxError:
            kind, err_msg = _classify_html_response(status, body or "")
            return ValidateOutcome(error=kind, working_xmlrpc_url=url, error_message=err_msg)

        if tree.xpath("//fault"):
            code_nodes = tree.xpath("//fault//member[name='faultCode']/value/int/text()")
            string_nodes = tree.xpath("//fault//member[name='faultString']/value/string/text()")
            code = code_nodes[0] if code_nodes else "?"
            msg = string_nodes[0] if string_nodes else "fault"
            kind, err_msg = _classify_fault(code, msg)
            return ValidateOutcome(error=kind, working_xmlrpc_url=url, error_message=err_msg)

        # Успех: ожидаем <params><param><value><array>...
        if tree.xpath("//params/param/value/array"):
            # Auth подтверждён — снимаем WP-роль через wp.getProfile (1 доп. POST,
            # только на валидных кредах). Best-effort: ошибка/fault не ломает OK.
            role = await self._fetch_role(url, login, password)
            if role is None:
                # Fallback: isAdmin из текущего ответа getUsersBlogs (без доп.
                # запроса). Если getProfile отключён/обрезан — хотя бы отличим
                # administrator от прочих.
                is_admin = tree.xpath("//member[name='isAdmin']/value/boolean/text()")
                if any(v.strip() == "1" for v in is_admin):
                    role = "administrator"
            return ValidateOutcome(error=ErrorKind.OK, working_xmlrpc_url=url, role=role)

        return ValidateOutcome(
            error=ErrorKind.UNKNOWN,
            working_xmlrpc_url=url,
            error_message="unexpected response shape",
        )

    async def _fetch_role(self, url: str, login: str, password: str) -> str | None:
        """WP-роль через wp.getProfile. Best-effort — любая ошибка → None."""
        try:
            resp = await self._client.post(
                url,
                content=_build_get_profile_xml(login, password),
                timeout=self._timeout,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                follow_redirects=True,
            )
        except Exception as e:
            log.debug("xmlrpc.getprofile.error", url=url, error=str(e))
            return None
        if resp.status_code >= 400 and resp.status_code != 405:
            return None
        return _parse_profile_role(resp.text or "")
