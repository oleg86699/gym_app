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

_sem = None
_sem_size = 0


def _semaphore(concurrency: int | None = None):
    """Ленивый общий семафор. concurrency задаётся один раз (из AppSettings/env).
    concurrency=None → переиспользуем уже инициализированный (не сбрасываем в
    дефолт): постинг праймит сем один раз на run, дальше дёргает с None."""
    global _sem, _sem_size
    if concurrency is None:
        if _sem is not None:
            return _sem
        size = _DEFAULT_CONCURRENCY
    else:
        size = max(1, concurrency)
    if _sem is None or size != _sem_size:
        import asyncio
        _sem = asyncio.Semaphore(size)
        _sem_size = size
    return _sem


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

    CANCELLATION-SAFE. Вся браузерная работа + закрытие вынесены в отдельную
    задачу `_run`, а внешнее ожидание (см. ниже) НЕ даёт отмене оборвать cleanup
    на полпути. Зачем: валидатор гоняет каждый cred под `asyncio.wait_for`
    (per-cred timeout). CF managed-challenge headful-браузером может длиться
    дольше этого таймаута → wait_for отменяет корутину ПРЯМО ВНУТРИ браузерной
    сессии. Раньше отмена рвала `await browser.close()` в finally → headful-
    chromium + node-драйвер утекали процессами (накопилось ~1.5k процессов,
    load average сервера → 231, nginx 502). Теперь отмена гасит `_run`, но его
    finally/`async with async_playwright` доводятся до конца → процессы не текут.
    (Постинг вызывает эту же функцию без wait_for — там отмены и не было.)"""
    if not is_browser_available():
        return None
    from patchright.async_api import async_playwright

    host = urlparse(base).netloc
    proxy_pw = _pw_proxy(proxy_url)
    sem = _semaphore(concurrency)

    async def _run() -> dict | None:
        async with async_playwright() as p:
            # headful (под Xvfb): CF Managed Challenge ("Just a moment…")
            # headless НЕ проходит (жёсткий 403); headful — проходит.
            browser = await p.chromium.launch(headless=False, args=_LAUNCH_ARGS)
            try:
                ctx_kw = {"ignore_https_errors": True}
                if proxy_pw:
                    ctx_kw["proxy"] = proxy_pw
                ctx = await browser.new_context(**ctx_kw)
                # NB: НЕ перехватываем запросы (ctx.route) — patchright теряет
                # stealth, и CF начинает блокировать. Картинки гасим launch-аргом.
                page = await ctx.new_page()
                ua = await page.evaluate("() => navigator.userAgent")
                logged = False
                for _ in range(2):  # cookie-гонка → reload + повтор
                    # domcontentloaded (не networkidle: CF-challenge держит
                    # соединения и networkidle висит до таймаута).
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
            finally:
                # ВСЕГДА закрываем браузер. wait_for(20): не виснуть в finally на
                # застрявшем graceful-close — драйвер всё равно добьёт chromium при
                # выходе из `async with async_playwright` (stop() убивает node-
                # процесс, а тот — своих детей-chromium).
                try:
                    await asyncio.wait_for(browser.close(), timeout=20)
                except Exception:
                    pass
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

    async with sem:
        task = asyncio.ensure_future(_run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Внешняя отмена (per-cred wait_for валидатора). Просим _run
            # завершиться и ДОЖИДАЕМСЯ его cleanup, не давая отмене оборвать
            # закрытие браузера на полпути. task отменяем РОВНО раз → его finally
            # (browser.close) и `async with` (stop драйвера) доигрывают штатно.
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cf_browser.login.error", host=host, error=str(e)[:200])
            return None


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
