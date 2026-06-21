"""curl_cffi транспорт — проходит CF/Akamai TLS-фингерпринт-блоки БЕЗ браузера
(impersonate Chrome). Слой между обычным httpx и FlareSolverr: большинство
«CF-блоков» это отпечаток TLS/HTTP2, а не JS-челлендж, и снимаются здесь
на ноль RAM. Если curl_cffi всё ещё видит CF — caller идёт в FlareSolverr.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

_CF_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "challenge-platform",
    "cf-error",
    "attention required",
    "verifying you are",
    "enable javascript and cookies",
    "/cdn-cgi/challenge-platform/",
)


def looks_cf_blocked(status: int, body: str | None) -> bool:
    """Похоже ли на CF/WAF блок (по статусу или маркерам в теле)."""
    low = (body or "")[:8192].lower()
    if any(m in low for m in _CF_BLOCK_MARKERS):
        return True
    return status in (403, 503)


async def cf_fetch(
    url: str,
    *,
    method: str = "GET",
    content: bytes | str | None = None,
    headers: dict | None = None,
    proxy: str | None = None,
    timeout: int = 30,
) -> tuple[int, str] | None:
    """GET/POST через curl_cffi с impersonate=chrome. Возвращает (status, text)
    или None при ошибке/отсутствии библиотеки. verify=False — у дешёвого WP-хостинга
    часто кривые сертификаты."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        log.debug("cf_transport.curl_cffi_missing")
        return None
    kw: dict = {"impersonate": "chrome", "timeout": timeout,
                "verify": False, "allow_redirects": True}
    if headers:
        kw["headers"] = headers
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    try:
        async with AsyncSession() as s:
            if method.upper() == "POST":
                r = await s.post(url, data=content, **kw)
            else:
                r = await s.get(url, **kw)
            return (r.status_code, r.text)
    except Exception as e:
        log.debug("cf_transport.error", url=url, method=method, error=str(e))
        return None


class _CfResp:
    """httpx-подобный shim над ответом curl_cffi — ровно то подмножество,
    что использует WpAdminClient (.status_code/.text/.headers/.url/.json())."""

    __slots__ = ("status_code", "text", "headers", "url", "_r")

    def __init__(self, r):
        self.status_code = r.status_code
        self.text = r.text
        self.headers = r.headers  # curl_cffi Headers — case-insensitive .get()
        self.url = str(getattr(r, "url", "") or "")
        self._r = r

    def json(self):
        return self._r.json()


class CfHttpxAdapter:
    """httpx.AsyncClient look-alike для подмножества, которое использует
    WpAdminClient (get/post/delete + timeout/follow_redirects/headers/data/json).

    Сначала пробует обёрнутый httpx-клиент; при CF/WAF TLS-фингерпринт-блоке
    прозрачно ретраит через ПЕРСИСТЕНТНУЮ curl_cffi (Chrome TLS) сессию, которая
    держит собственный cookie-jar — поэтому полная wp-admin login-сессия может
    целиком пройти через curl_cffi, когда httpx режется по фингерпринту.
    Sticky: как только curl_cffi сработал — последующие вызовы идут сразу в него
    (без лишнего 403-раунда по httpx).

    Drop-in: WpAdminClient оборачивает свой httpx-клиент в этот адаптер, все
    существующие self._client.get/post/delete(...) остаются без изменений.
    proxy_url — тот же резидентский exit, что у httpx-клиента (CF режет
    datacenter-IP по репутации даже с верным TLS, поэтому curl_cffi обязан
    идти тем же proxy)."""

    def __init__(self, httpx_client, proxy_url: str | None = None):
        self._httpx = httpx_client
        self._proxy = proxy_url
        self._cf = None  # ленивая персистентная curl_cffi AsyncSession
        self._cf_sticky = False  # True → идём сразу в curl_cffi
        self._cf_ok = True  # False если curl_cffi не установлен

    # passthrough свойств, которые читает WpAdminClient
    @property
    def headers(self):
        return self._httpx.headers

    @property
    def cookies(self):
        return self._httpx.cookies

    async def _session(self):
        if self._cf is None:
            from curl_cffi.requests import AsyncSession

            self._cf = AsyncSession()  # cookies персистятся между запросами
        return self._cf

    async def _httpx_call(self, method, url, *, headers, data, json, follow_redirects, timeout):
        hkw: dict = {"follow_redirects": follow_redirects}
        if timeout is not None:
            hkw["timeout"] = timeout
        if headers is not None:
            hkw["headers"] = headers
        if method == "POST":
            if data is not None:
                hkw["data"] = data
            if json is not None:
                hkw["json"] = json
            return await self._httpx.post(url, **hkw)
        if method == "DELETE":
            return await self._httpx.delete(url, **hkw)
        return await self._httpx.get(url, **hkw)

    async def _cf_call(self, method, url, *, headers, data, json, follow_redirects, timeout):
        if not self._cf_ok:
            return None
        try:
            s = await self._session()
        except ImportError:
            self._cf_ok = False
            log.debug("admin.cf.curl_cffi_missing")
            return None
        kw: dict = {"impersonate": "chrome", "verify": False,
                    "allow_redirects": follow_redirects, "timeout": timeout or 30}
        if self._proxy:
            kw["proxies"] = {"http": self._proxy, "https": self._proxy}
        if headers:
            kw["headers"] = headers
        try:
            if method == "POST":
                r = await s.post(url, data=data, json=json, **kw)
            elif method == "DELETE":
                r = await s.delete(url, **kw)
            else:
                r = await s.get(url, **kw)
            return _CfResp(r)
        except Exception as e:
            log.debug("admin.cf.error", url=url, method=method, error=str(e)[:200])
            return None

    async def _dispatch(self, method, url, *, headers=None, data=None, json=None,
                        follow_redirects=True, timeout=None):
        # Sticky: сайт уже опознан как CF-блок для httpx → сразу curl_cffi
        if self._cf_sticky and self._cf_ok:
            alt = await self._cf_call(method, url, headers=headers, data=data, json=json,
                                      follow_redirects=follow_redirects, timeout=timeout)
            if alt is not None:
                return alt
            # curl_cffi не смог в этот раз — один плоский httpx-заход (без ре-ретрая)
            return await self._httpx_call(method, url, headers=headers, data=data, json=json,
                                          follow_redirects=follow_redirects, timeout=timeout)
        # Primary: httpx
        resp = await self._httpx_call(method, url, headers=headers, data=data, json=json,
                                      follow_redirects=follow_redirects, timeout=timeout)
        # CF/WAF блок? ретрай тем же запросом через curl_cffi и залипаем, если помогло
        if self._cf_ok and looks_cf_blocked(resp.status_code, resp.text):
            alt = await self._cf_call(method, url, headers=headers, data=data, json=json,
                                      follow_redirects=follow_redirects, timeout=timeout)
            if alt is not None and not looks_cf_blocked(alt.status_code, alt.text):
                if not self._cf_sticky:
                    log.info("admin.cf.bypass_curlcffi", url=url, method=method)
                self._cf_sticky = True
                return alt
        return resp

    async def get(self, url, **kw):
        return await self._dispatch("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._dispatch("POST", url, **kw)

    async def delete(self, url, **kw):
        return await self._dispatch("DELETE", url, **kw)

    async def aclose(self):
        if self._cf is not None:
            try:
                await self._cf.close()
            except Exception:
                pass
