"""Patchright-логин + сессия для CF Bot-Fight сайтов.

Идея (проверено PoC): pure-HTTP режется CF (403), переиспользуемого cf_clearance
нет. Но реальный браузер проходит CF и логинится; cookies WP-сессии
(wordpress_logged_in) + curl_cffi(impersonate=chrome) + тот же UA/IP дальше
пускают в /wp-admin/ запросами. Поэтому браузер нужен ОДИН раз на сайт (логин),
а постинг — обычными запросами.

Patchright импортируется ЛЕНИВО: если не установлен (например, в API-контейнере),
is_browser_available() == False и Tier 3 просто недоступен — остальной код цел.
"""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse

import structlog

log = structlog.get_logger(__name__)

# Кап одновременных браузер-контекстов. Phase 2: будет читаться из AppSettings
# (cf_browser_concurrency); пока — env с дефолтом 3 (как у FlareSolverr).
_DEFAULT_CONCURRENCY = int(os.getenv("CF_BROWSER_CONCURRENCY", "3"))

# Сессия WP живёт днями; кеш держим консервативно (перелогин дешёвый).
SESSION_TTL_SEC = int(os.getenv("CF_SESSION_TTL_SEC", str(6 * 3600)))

# Картинки отключаем НАТИВНО launch-аргументом (без перехвата запросов → не
# палится анти-ботом). script/CSS НЕ трогаем — CF-челлендж это JS.
# Запросы через ctx.route НЕ перехватываем вообще: patchright теряет stealth.
_LAUNCH_ARGS = [
    "--blink-settings=imagesEnabled=false",
    "--disable-blink-features=AutomationControlled",
]

# ─── Пул переиспользуемых браузеров (корневой фикс утечки, модель bap) ──
# Раньше: launch(browser) на КАЖДЫЙ логин → при отмене per-cred wait_for или
# краше драйвера (голодание FD/shm) chromium сиротел, накапливался до ~1000 → OOM.
# Теперь: ОДИН долгоживущий браузер на процесс-воркер, на задачу — только
# new_context() (свой proxy/UA/куки, чистая сессия), context.close() на выходе.
# browser.close() — лишь на recycle-after-N. Контекст закрыть дёшево и безопасно
# даже под отменой → прежний drive-to-completion больше не нужен. Анти-детект не
# страдает: CF видит контекст (куки/IP/поведение), не PID процесса (callout bap).

# Пересоздаём браузер каждые N выданных контекстов (память/фрагментация драйвера).
_RECYCLE_AFTER = int(os.getenv("CF_BROWSER_RECYCLE_AFTER", "50"))
# Сколько ждём слот семафора, прежде чем счесть контексты зомби и форс-рециклить.
_ACQUIRE_TIMEOUT_S = int(os.getenv("CF_BROWSER_ACQUIRE_TIMEOUT_S", "120"))
_CLOSE_TIMEOUT_S = 15

# Подстроки «мёртвого транспорта» драйвера Playwright → браузер/драйвер надо
# рециклить (иначе следующие вызовы висят/сиротят chromium).
_DEAD_TRANSPORT = (
    "writeunixtransport", "readunixtransport", "the handler is closed",
    "connection closed", "target closed", "browser has been closed",
    "browser has disconnected", "has been closed", "target page, context",
)


def _is_dead_transport(exc: BaseException) -> bool:
    m = str(exc).lower()
    return any(s in m for s in _DEAD_TRANSPORT)


class _BrowserPool:
    """Один long-lived браузер на процесс + контекст-на-задачу (модель bap).

    Семафор ограничивает одновременные КОНТЕКСТЫ (не запуски браузера). Браузер
    ленивый синглтон; на dead-transport/emergency — рецикл драйвера+браузера."""

    def __init__(self) -> None:
        self._pw = None            # async_playwright() manager
        self._pw_ctx = None        # entered playwright (chromium.launch source)
        self._browser = None
        self._lock = asyncio.Lock()          # сериализует launch/recycle
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._active = 0           # живых контекстов сейчас
        self._task_count = 0       # выдано контекстов с последнего recycle
        self._bg_tasks: set = set()  # фоновые close/recycle (защита от GC)

    def _ensure_sem(self, concurrency: int | None) -> asyncio.Semaphore:
        """Размер задаётся один раз (из cf_browser_concurrency); None → как есть."""
        if concurrency is None:
            if self._sem is not None:
                return self._sem
            size = _DEFAULT_CONCURRENCY
        else:
            size = max(1, concurrency)
        if self._sem is None or size != self._sem_size:
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    async def _stop_pw_locked(self) -> None:
        """Полный teardown (browser + playwright). Под _lock. Для dead-transport."""
        b, self._browser = self._browser, None
        if b is not None:
            try:
                await asyncio.wait_for(b.close(), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass
        pw, self._pw, self._pw_ctx = self._pw, None, None
        if pw is not None:
            try:
                await asyncio.wait_for(pw.__aexit__(None, None, None), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass

    async def _close_browser_locked(self) -> None:
        """Закрыть только браузер, playwright оставить (для recycle-after-N)."""
        b, self._browser = self._browser, None
        if b is not None:
            try:
                await asyncio.wait_for(b.close(), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass

    async def _launch_locked(self) -> None:
        """Поднять браузер (и playwright, если надо). Под _lock. Ретрай через
        полный teardown, если драйвер мёртв."""
        from patchright.async_api import async_playwright
        for attempt in (1, 2):
            try:
                if self._pw_ctx is None:
                    self._pw = async_playwright()
                    self._pw_ctx = await self._pw.__aenter__()
                # headful (под Xvfb): CF Managed Challenge headless не проходит.
                self._browser = await self._pw_ctx.chromium.launch(
                    headless=False, args=_LAUNCH_ARGS)
                self._task_count = 0
                log.info("cf_browser.pool.launched", attempt=attempt)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("cf_browser.pool.launch_failed",
                            attempt=attempt, error=str(e)[:150])
                await self._stop_pw_locked()
                if attempt == 2:
                    raise

    async def _ensure_browser(self):
        b = self._browser
        if b is not None:
            try:
                if b.is_connected():
                    return b
            except Exception:
                pass
        async with self._lock:
            b = self._browser
            if b is not None:
                try:
                    if b.is_connected():
                        return b
                except Exception:
                    pass
            await self._launch_locked()
            return self._browser

    async def acquire(self, ctx_kw: dict, concurrency: int | None):
        """Занять слот и вернуть свежий context. sem держится до release()."""
        sem = self._ensure_sem(concurrency)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=_ACQUIRE_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Все слоты держат зомби-контексты → форс-закрываем браузер (закрытие
            # добивает зомби, их задачи отвалятся и вернут слоты через release()).
            log.warning("cf_browser.pool.acquire_timeout.recycle")
            async with self._lock:
                await self._stop_pw_locked()
            await asyncio.wait_for(sem.acquire(), timeout=_ACQUIRE_TIMEOUT_S)
        # Создать контекст, один ретрай на мёртвый браузер/драйвер.
        for attempt in (1, 2):
            try:
                browser = await self._ensure_browser()
                context = await browser.new_context(**ctx_kw)
                self._active += 1
                self._task_count += 1
                return context
            except Exception as e:  # noqa: BLE001
                if attempt == 1 and _is_dead_transport(e):
                    log.warning("cf_browser.pool.dead_transport.recycle",
                                error=str(e)[:120])
                    async with self._lock:
                        await self._stop_pw_locked()
                    continue
                sem.release()
                raise

    def release_nowait(self, context) -> None:
        """Вернуть слот НЕМЕДЛЕННО (sync) + закрыть context в фоне. Зовётся из
        finally, в т.ч. под отменой — потому без await: sem.release() sync-гарантия,
        а close/recycle уходят отдельной задачей, переживающей отмену вызывающего."""
        self._active = max(0, self._active - 1)
        if self._sem is not None:
            self._sem.release()
        t = asyncio.ensure_future(self._close_and_maybe_recycle(context))
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def _close_and_maybe_recycle(self, context) -> None:
        try:
            await asyncio.wait_for(context.close(), timeout=_CLOSE_TIMEOUT_S)
        except Exception:
            pass
        # recycle-after-N: реальный browser.close только когда нет живых контекстов
        # → in-flight задачи не рвём.
        if self._task_count >= _RECYCLE_AFTER and self._active == 0:
            async with self._lock:
                if self._task_count >= _RECYCLE_AFTER and self._active == 0:
                    try:
                        await self._close_browser_locked()
                        await self._launch_locked()
                    except Exception as e:  # noqa: BLE001
                        log.warning("cf_browser.pool.recycle_failed", error=str(e)[:150])


_pool: _BrowserPool | None = None


def _get_pool() -> _BrowserPool:
    """Ленивый синглтон пула на процесс (Lock/Semaphore создаются в live-loop)."""
    global _pool
    if _pool is None:
        _pool = _BrowserPool()
    return _pool


def prime_concurrency(concurrency: int | None) -> None:
    """Задать размер семафора контекстов один раз (из cf_browser_concurrency).
    Постинг зовёт это один раз на run; дальше browser_login_session(concurrency=None)
    переиспользует уже настроенный пул."""
    _get_pool()._ensure_sem(concurrency)


def is_browser_available() -> bool:
    """Установлен ли Patchright (+ браузер). Если нет — Tier 3 пропускаем."""
    try:
        import patchright.async_api  # noqa: F401
        return True
    except Exception:
        return False


# ─── Redis-кеш сессии (cookies+UA по домену+proxy) ────────────────────


def _proxy_sig(proxy_url: str | None) -> str:
    if not proxy_url:
        return "noproxy"
    import hashlib
    return hashlib.sha1(proxy_url.encode()).hexdigest()[:12]


async def _redis():
    try:
        import redis.asyncio as aioredis

        from core.config import settings
        return aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as e:
        log.debug("cf_browser.redis.unavailable", error=str(e))
        return None


async def get_cached_session(host: str, proxy_url: str | None) -> dict | None:
    """Сессия {cookies, user_agent} для (host, proxy) или None."""
    r = await _redis()
    if not r:
        return None
    try:
        raw = await r.get(f"cf:session:{host}:{_proxy_sig(proxy_url)}")
        if raw:
            data = json.loads(raw)
            if data.get("cookies"):
                log.info("cf_browser.session.cache_hit", host=host)
                return data
    except Exception as e:
        log.debug("cf_browser.session.read_failed", error=str(e))
    finally:
        try:
            await r.aclose()
        except Exception:
            pass
    return None


async def cache_session(host: str, proxy_url: str | None, session: dict) -> None:
    if not session or not session.get("cookies"):
        return
    r = await _redis()
    if not r:
        return
    try:
        await r.set(f"cf:session:{host}:{_proxy_sig(proxy_url)}",
                    json.dumps(session), ex=SESSION_TTL_SEC)
        log.info("cf_browser.session.cached", host=host, cookies=len(session["cookies"]))
    except Exception as e:
        log.debug("cf_browser.session.write_failed", error=str(e))
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


# ─── Браузерный логин (Patchright) ────────────────────────────────────


def _pw_proxy(proxy_url: str | None):
    if not proxy_url:
        return None
    u = urlparse(proxy_url)
    return {"server": f"{u.scheme}://{u.hostname}:{u.port}",
            "username": u.username or "", "password": u.password or ""}


async def browser_login_session(
    base: str, login: str, password: str, *,
    proxy_url: str | None = None, concurrency: int | None = None,
    timeout_ms: int = 60000,
) -> dict | None:
    """Пройти CF + залогиниться браузером, вернуть сессию {cookies, user_agent}
    (для curl_cffi-реплея) либо None. Идемпотентно кеширует сессию.

    Обрабатывает cookie-гонку WordPress (флапающий «cookies are blocked»):
    при неудаче — reload + повтор сабмита.

    Берёт контекст из общего пула (_BrowserPool): браузер переиспользуется, на
    задачу — только свой new_context() (proxy/UA/куки, чистая сессия).

    CANCELLATION-SAFE без трюков. Валидатор гоняет каждый cred под
    `asyncio.wait_for` (per-cred timeout); CF managed-challenge headful-браузером
    может длиться дольше → отмена рвёт нас на любом await. Но finally всегда
    возвращает контекст в пул СИНХРОННО (release_nowait: sem.release + фоновое
    context.close), а браузер живёт дальше для других задач. Раньше тут
    закрывался ВЕСЬ браузер per-call → под отменой это текло процессами (копилось
    ~1.5k, load average → 231, nginx 502); пул это устранил в корне.
    (Постинг зовёт эту же функцию без wait_for — работает так же.)"""
    if not is_browser_available():
        return None

    host = urlparse(base).netloc
    proxy_pw = _pw_proxy(proxy_url)
    pool = _get_pool()
    ctx_kw: dict = {"ignore_https_errors": True}
    if proxy_pw:
        ctx_kw["proxy"] = proxy_pw

    ctx = None
    logged = False
    cookies: list = []
    ua = ""
    try:
        ctx = await pool.acquire(ctx_kw, concurrency)
        # NB: НЕ перехватываем запросы (ctx.route) — patchright теряет stealth,
        # и CF начинает блокировать. Картинки гасим launch-аргом браузера.
        page = await ctx.new_page()
        ua = await page.evaluate("() => navigator.userAgent")
        for _ in range(2):  # cookie-гонка → reload + повтор
            # domcontentloaded (не networkidle: CF-challenge держит соединения
            # и networkidle висит до таймаута).
            await page.goto(f"{base}/wp-login.php",
                            wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                # ждём пока CF managed-challenge авто-решится → форма логина
                await page.wait_for_selector("#user_login", timeout=40000)
            except Exception:
                break
            await page.fill("#user_login", login)
            await page.fill("#user_pass", password)
            await page.click("#wp-submit")
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            cks = await ctx.cookies()
            if (any("wordpress_logged_in" in c["name"] for c in cks)
                    or "/wp-admin" in page.url):
                logged = True
                break
        cookies = await ctx.cookies()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("cf_browser.login.error", host=host, error=str(e)[:200])
        return None
    finally:
        # Синхронный возврат слота — переживает отмену (без await в finally).
        if ctx is not None:
            pool.release_nowait(ctx)

    if not logged:
        log.info("cf_browser.login.failed", host=host)
        return None
    session = {"cookies": [{"name": c["name"], "value": c["value"],
                            "domain": c.get("domain") or host, "path": c.get("path") or "/"}
                           for c in cookies],
               "user_agent": ua}
    await cache_session(host, proxy_url, session)
    log.info("cf_browser.login.ok", host=host, cookies=len(session["cookies"]))
    return session


# ─── curl_cffi-реплей запросом с сессией ──────────────────────────────


async def replay_request(
    url: str, session: dict, *, method: str = "GET", proxy_url: str | None = None,
    data=None, timeout: int = 30, allow_redirects: bool = True,
    headers: dict | None = None,
):
    """Запрос через curl_cffi(impersonate=chrome) с куками+UA сессии (тот же
    proxy/IP). Возвращает curl_cffi Response или None. Сессия не переносится →
    302 на /wp-login.php / CF-блок (caller проверяет).
    allow_redirects=False — чтобы прочитать Location (admin post.php → 302)."""
    try:
        from curl_cffi.requests import AsyncSession
    except Exception:
        return None
    host = urlparse(url).netloc
    s = AsyncSession()
    try:
        for c in session.get("cookies", []):
            try:
                s.cookies.set(c["name"], c["value"], domain=c.get("domain") or host,
                              path=c.get("path") or "/")
            except Exception:
                pass
        hdrs = {"User-Agent": session.get("user_agent", "")}
        if headers:
            hdrs.update(headers)
        kw = {"impersonate": "chrome", "verify": False, "timeout": timeout,
              "allow_redirects": allow_redirects, "headers": hdrs}
        if proxy_url:
            kw["proxies"] = {"http": proxy_url, "https": proxy_url}
        if method.upper() == "POST":
            return await s.post(url, data=data, **kw)
        return await s.get(url, **kw)
    except Exception as e:  # noqa: BLE001
        log.debug("cf_browser.replay.error", url=url, error=str(e)[:150])
        return None
    finally:
        try:
            await s.close()
        except Exception:
            pass


# ─── Постинг через кешированную сессию (curl_cffi, classic admin form) ──


async def post_via_session(
    base: str, session: dict, *, title: str, content: str,
    status: str = "publish", proxy_url: str | None = None, timeout: int = 40,
) -> dict:
    """Создать пост через wp-admin classic-форму, переиспользуя браузер-сессию
    (curl_cffi replay). Зеркалит WpAdminClient.create_post, но поверх CF.

    Возвращает dict:
      {"status":"ok", "post_id":int|None, "posted_url":str|None}
      {"status":"expired"}                       — куки протухли (302 на login)
      {"status":"error", "error":str}            — прочее (включая Gutenberg)
    """
    new_url = f"{base}/wp-admin/post-new.php"
    r = await replay_request(new_url, session, proxy_url=proxy_url, timeout=timeout)
    if r is None:
        return {"status": "error", "error": "post-new.php: no response (CF?)"}
    final_url = str(getattr(r, "url", "") or "")
    if "wp-login.php" in final_url or getattr(r, "status_code", 0) in (401, 403):
        return {"status": "expired"}
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(r.text)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"bad html: {e}"}

    post_id_node = tree.xpath('//input[@id="post_ID"]/@value')
    nonce_node = tree.xpath('//input[@id="_wpnonce"]/@value')
    referer_node = tree.xpath('//input[@name="_wp_http_referer"]/@value')
    user_id_node = tree.xpath('//input[@id="user_ID"]/@value')
    if not nonce_node or not post_id_node:
        # Gutenberg (block editor) — classic-полей нет. Пока не поддержано тут.
        return {"status": "error", "error": "gutenberg/no classic nonce"}

    post_id = post_id_node[0]
    nonce = nonce_node[0]
    referer = referer_node[0] if referer_node else "/wp-admin/post-new.php"
    user_id = user_id_node[0] if user_id_node else ""
    form = {
        "_wpnonce": nonce, "_wp_http_referer": referer, "user_ID": user_id,
        "action": "editpost", "originalaction": "editpost", "post_author": user_id,
        "post_type": "post", "original_post_status": "auto-draft", "post_ID": post_id,
        "post_title": title or "", "content": content, "post_status": status,
        "visibility": "public", "publish": "Publish",
    }
    pr = await replay_request(
        f"{base}/wp-admin/post.php", session, method="POST", data=form,
        proxy_url=proxy_url, timeout=timeout, allow_redirects=False,
        headers={"Referer": f"{base}{referer}"},
    )
    if pr is None:
        return {"status": "error", "error": "post.php: no response (CF?)"}
    code = getattr(pr, "status_code", 0)
    if code in (301, 302):
        loc = pr.headers.get("Location", "") if getattr(pr, "headers", None) else ""
        if "wp-login.php" in loc:
            return {"status": "expired"}
        if "post=" in loc and "action=edit" in loc:
            try:
                pid = int(loc.split("post=")[1].split("&")[0])
            except (ValueError, IndexError):
                pid = None
            return {"status": "ok", "post_id": pid,
                    "posted_url": f"{base}/?p={pid}" if pid else None}
        return {"status": "error", "error": f"unexpected redirect: {loc[:120]}"}
    return {"status": "error", "error": f"post.php HTTP {code}"}
