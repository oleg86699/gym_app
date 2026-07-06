"""
Валидатор WP-credentials (пул).

FULL-валидация в ДВА tier-а (parity с батч-валидатором — единая семантика
«только full», см. domain/wp_batches/service.run_batch_validation):

- Tier 1 — XML-RPC (`XmlRpcPoster.validate` / wp.getUsersBlogs): ставит
  can_xmlrpc / can_post_via_xmlrpc + is_valid по auth-ответу.
- Tier 2 — admin form-login (`WpAdminClient.login`, + Patchright-браузер для
  CF): подтверждает can_admin_login + роль. Именно он растит admin-канал и
  чинит пул под ссылки. Без него кнопка Validate щупала только XML-RPC.

Оба tier-а идут через ПУЛ прокси (ротация по кредам) — admin-login с серверного
IP легко ловит баны. Состояние прогресса — в Redis (DB 9), UI видит его в
global queue без БД-нагрузки.

Применяется в:
- TaskIQ scheduled cron: stale-only (раз в 4 часа).
- On-demand TaskIQ task: scope='all' | 'invalid' | 'transient' | 'stale' — из UI.

Отличие от батч-валидатора: без тяжёлых capability-probe (theme-editor/widgets/
wp_version), lang-detect и inline-provision — здесь нужен только вердикт двух
каналов. Всё это остаётся эксклюзивом батча.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import redis.asyncio as aioredis
import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.crypto import decrypt_password
from core.db import WriteSession
from infrastructure.db.models import Proxy, WpCredential, WpSite
from infrastructure.wp_client import ErrorKind, ValidateOutcome, XmlRpcPoster

log = structlog.get_logger(__name__)

# Порог error_counter, после которого credential помечаем is_valid=False.
INVALIDATE_THRESHOLD = 5

# Считаем credential «stale» если последняя валидация была давно (или никогда).
STALE_HOURS = 4

# Параллелизм валидации. Не больше — wp.getUsersBlogs всё ещё нагружает целевые
# сайты. Тайм-аут на запрос отдельный.
VALIDATE_CONCURRENCY = 20
VALIDATE_TIMEOUT_S = 20

# Жёсткий per-cred таймаут на весь full-цикл (Tier1 + Tier2 + возможный браузер).
# Один зависший cred иначе держал бы слот семафора навсегда.
VALIDATE_PER_CRED_TIMEOUT_S = 150

# Кап прокси-пула на прогон (как в батч-валидаторе).
_MAX_PROXY_POOL = 64


# ─── Redis state (singleton key) ─────────────────────────────────────


def _state_redis_url() -> str:
    base = settings.REDIS_URL.rsplit("/", 1)[0]
    return f"{base}/9"


_state_client: aioredis.Redis | None = None


def _get_state_client() -> aioredis.Redis:
    global _state_client
    if _state_client is None:
        _state_client = aioredis.from_url(_state_redis_url(), decode_responses=True)
    return _state_client


_STATE_KEY = "wp_validation:state"


@dataclass
class ValidationState:
    running: bool = False
    scope: str = "all"
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    done: int = 0
    valid: int = 0
    invalid: int = 0
    transient_errors: int = 0
    last_actor_id: int | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str | None) -> "ValidationState":
        if not raw:
            return cls()
        try:
            return cls(**json.loads(raw))
        except Exception:
            return cls()


async def get_state() -> ValidationState:
    raw = await _get_state_client().get(_STATE_KEY)
    return ValidationState.from_json(raw)


async def _save_state(state: ValidationState) -> None:
    await _get_state_client().set(_STATE_KEY, state.to_json(), ex=24 * 3600)


# ─── Per-credential validate ─────────────────────────────────────────


async def _apply_validation_result(
    session: AsyncSession, cred: WpCredential, outcome: ValidateOutcome
) -> tuple[bool, bool, bool]:
    """
    Применить результат к credential + site. Возвращает (was_valid_change,
    is_valid_after, is_transient).
    """
    now = datetime.now(UTC)
    values: dict = {"last_validated_at": now}
    was_valid = cred.is_valid
    is_transient = False
    new_valid = was_valid

    # Точный диагноз для UI (parity с batch-валидатором).
    raw_kind = outcome.error.value if outcome.error else "unknown"
    values["last_validation_kind"] = raw_kind
    values["last_error_message"] = (
        outcome.error_message[:500] if outcome.error_message else None
    )

    # Capability matrix (XML-RPC channel) — parity с batch-валидатором
    # (см. wp_batches/service._apply_validation_result). Раньше pool-валидатор
    # НЕ трогал can_* → admin/rpc каналы застывали. Теперь Tier 1 честно пишет rpc.
    if outcome.error == ErrorKind.OK and outcome.valid_via == "admin_browser":
        # CF-сайт: прошли Patchright-логином, XML-RPC не проверяли → rpc не трогаем.
        values["can_admin_login"] = True
        values["can_post_via_admin"] = True
        values["last_admin_check_at"] = now
    elif outcome.error == ErrorKind.OK:
        values["can_xmlrpc"] = True
        values["can_post_via_xmlrpc"] = True
        if outcome.role:
            values["admin_role"] = outcome.role
            values["can_create_users"] = outcome.role == "administrator"
    elif outcome.error in (ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED):
        values["can_xmlrpc"] = True
        values["can_post_via_xmlrpc"] = False
    elif outcome.error == ErrorKind.XMLRPC_DISABLED:
        values["can_xmlrpc"] = False
        values["can_post_via_xmlrpc"] = False

    if outcome.success:
        values["is_valid"] = True
        values["error_counter"] = 0
        values["last_error_message"] = None
        new_valid = True
    elif outcome.error in (ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED):
        new_err_counter = (cred.error_counter or 0) + 1
        values["error_counter"] = new_err_counter
        if new_err_counter >= INVALIDATE_THRESHOLD:
            values["is_valid"] = False
            new_valid = False
    else:
        # NETWORK/SERVER_ERROR/UNKNOWN/SITE_NOT_FOUND/XMLRPC_DISABLED/PARKED/TASK_TIMEOUT —
        # transient, флаги не трогаем чтобы не выкидывать рабочие credentials из-за времянки.
        is_transient = True

    await session.execute(
        update(WpCredential).where(WpCredential.id == cred.id).values(**values)
    )

    # Если discovery нашёл рабочий URL — сохраним для будущих запросов
    if outcome.working_xmlrpc_url and cred.site is not None:
        await session.execute(
            update(WpSite)
            .where(WpSite.id == cred.site.id)
            .values(last_working_url=outcome.working_xmlrpc_url, last_working_at=now)
        )

    await session.commit()
    return was_valid != new_valid, new_valid, is_transient


# ─── Tier 2: admin form-login (растит admin-канал) ───────────────────


async def _cf_browser_login(
    cred: WpCredential, pw: str, proxy_url: str | None, cf_conc: int
) -> bool:
    """Tier 3: один раз проходим CF + логинимся браузером (Patchright) на том же
    прокси/IP. Успех = cred валиден через admin-канал (сессия кешируется в Redis,
    постинг переиспользует curl_cffi-реплеем). True если зашли. Зеркало
    wp_batches/service._cf_browser_login."""
    from infrastructure.cf_browser import browser_login_session, is_browser_available

    if not is_browser_available() or not cred.site:
        return False
    from urllib.parse import urlparse as _urlparse

    base = cred.site.last_working_url or f"https://{cred.site.domain}"
    _p = _urlparse(base)
    base = f"{_p.scheme}://{_p.netloc}" if _p.netloc else f"https://{cred.site.domain}"
    try:
        sess = await browser_login_session(
            base, cred.login, pw, proxy_url=proxy_url, concurrency=cf_conc,
        )
        return sess is not None
    except Exception as e:  # noqa: BLE001
        log.warning("pool_validate.cf_browser.error", cred_id=cred.id, error=str(e)[:200])
        return False


async def _tier2_admin_login(
    cred: WpCredential,
    tier1_outcome: ValidateOutcome,
    http: httpx.AsyncClient,
    proxy_url: str | None,
    rate_limiter,
    cf_conc: int,
) -> str | None:
    """
    Tier 2 — wp-admin form-login через тот же прокси/клиент, что и Tier 1
    (+ Patchright-браузер для CF). Ставит can_admin_login / роль и разрешает
    is_valid когда Tier 1 был inconclusive. Возвращает влияние на ИТОГОВЫЙ вердикт
    для счётчиков state: 'valid' | 'invalid' | None (None = вердикт не изменил,
    считаем по Tier 1).

    Упрощённая версия batch-Tier2: без capability-probe / provision / lang —
    здесь нужен только admin-канал + вердикт. Логика инвалидации/сохранения
    prior-trust — зеркало wp_batches/service._maybe_run_tier2.
    """
    from infrastructure.wp_admin_client import (
        AdminLoginKind,
        LoginOutcome,
        WpAdminClient,
    )

    if cred.site is None:
        return None

    client = WpAdminClient(http, timeout_seconds=VALIDATE_TIMEOUT_S, proxy_url=proxy_url)
    via_browser = False
    pw = decrypt_password(cred.password)
    try:
        await rate_limiter.acquire(cred.site.domain)
        login_res = await client.login(site=cred.site, login=cred.login, password=pw)
        # Tier 3 (браузер): CF/generic-WAF (403/JS-interstitial/503) — лёгкого
        # HTTP-обхода нет, проходим браузером один раз, он же логинится.
        _BROWSER_TRIGGERS = {
            AdminLoginKind.CF_CHALLENGE,
            AdminLoginKind.UNKNOWN,
            AdminLoginKind.SERVER_ERROR,
        }
        if login_res.error in _BROWSER_TRIGGERS:
            if await _cf_browser_login(cred, pw, proxy_url, cf_conc):
                via_browser = True
                login_res = LoginOutcome(
                    error=AdminLoginKind.OK, error_message="passed via browser (CF)",
                )
    except Exception as e:  # noqa: BLE001
        log.warning("pool_validate.tier2.error", cred_id=cred.id, error=str(e)[:200])
        return None

    now = datetime.now(UTC)
    cred_values: dict = {"last_admin_check_at": now}
    site_values: dict = {}
    tier1_decisive = tier1_outcome.error in (
        ErrorKind.OK, ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED,
    )
    result: str | None = None

    if login_res.error == AdminLoginKind.OK:
        cred_values["can_admin_login"] = True
        cred_values["is_valid"] = True
        cred_values["error_counter"] = 0
        cred_values["last_error_at"] = None
        cred_values["error_cooldown_until"] = None
        if tier1_outcome.error != ErrorKind.OK:
            cred_values["last_error_message"] = None
        result = "valid"
        if via_browser:
            # Прошли браузером (CF) — REST/probe опять упрутся в CF. Ставим
            # admin-канал напрямую, постинг пойдёт Tier 3 replay-ом.
            cred_values["can_post_via_admin"] = True
            site_values["cf_protected"] = True
        else:
            # Роль + create_users через REST users/me — дёшево (1 запрос).
            try:
                role, caps_map = await client.fetch_role_and_caps(cred.site)
                if role:
                    cred_values["admin_role"] = role
                if caps_map:
                    cred_values["can_create_users"] = bool(caps_map.get("create_users"))
            except Exception as e:  # noqa: BLE001
                log.debug("pool_validate.role_probe.failed", cred_id=cred.id, error=str(e))
    elif login_res.error in (AdminLoginKind.AUTH_INVALID, AdminLoginKind.PERMISSION_DENIED):
        cred_values["can_admin_login"] = False
        # Инвалидируем только если Tier 1 (XML-RPC) сам не подтвердил валидность:
        # admin-login мог упасть из-за captcha/2FA/IP, а cred рабочая через XML-RPC.
        if tier1_outcome.error != ErrorKind.OK:
            cred_values["is_valid"] = False
            result = "invalid"
    elif login_res.error == AdminLoginKind.CF_CHALLENGE:
        site_values["cf_protected"] = True
        if not tier1_decisive and cred.can_admin_login is not True:
            cred_values["is_valid"] = False
            result = "invalid"
    elif login_res.error == AdminLoginKind.PARKED:
        cred_values["can_admin_login"] = False
        cred_values["is_valid"] = False
        result = "invalid"
    else:
        # NETWORK / LOGIN_DISABLED / UNKNOWN / SERVER_ERROR / SITE_NOT_FOUND —
        # inconclusive. not_confirmed только если Tier 1 тоже не decisive И нет
        # prior can_admin_login=True (сохраняем доверие к прошлому подтверждению).
        if not tier1_decisive and cred.can_admin_login is not True:
            cred_values["is_valid"] = False
            result = "invalid"

    # Сайт ОТВЕТИЛ через админку (ok / явный auth-fail) → он жив, сбрасываем
    # site-счётчик провалов (мог накрутиться на Tier 1 xmlrpc_disabled/CF).
    if login_res.error in (
        AdminLoginKind.OK, AdminLoginKind.AUTH_INVALID, AdminLoginKind.PERMISSION_DENIED,
    ):
        site_values["consecutive_site_failures"] = 0
        site_values["last_site_failure_at"] = None
        site_values["last_site_failure_kind"] = None

    async with WriteSession() as s_t2:
        await s_t2.execute(
            update(WpCredential).where(WpCredential.id == cred.id).values(**cred_values)
        )
        if site_values and cred.site:
            await s_t2.execute(
                update(WpSite).where(WpSite.id == cred.site.id).values(**site_values)
            )
        await s_t2.commit()
    return result


# ─── Bulk runner ─────────────────────────────────────────────────────


async def _iter_creds_to_validate(
    session: AsyncSession, scope: str
) -> AsyncIterator[WpCredential]:
    """
    scope:
      - 'all' — все credentials с активным сайтом
      - 'invalid' — is_valid=False
      - 'stale' — last_validated_at < now-STALE_HOURS (включая NULL)
    """
    after_id = 0
    batch = 200
    threshold = datetime.now(UTC) - timedelta(hours=STALE_HOURS)
    while True:
        stmt = (
            select(WpCredential)
            .where(
                WpCredential.deleted_at.is_(None),
                WpCredential.id > after_id,
            )
            .options(selectinload(WpCredential.site))
            .order_by(WpCredential.id)
            .limit(batch)
        )
        if scope == "invalid":
            stmt = stmt.where(WpCredential.is_valid.is_(False))
        elif scope == "transient":
            # inconclusive результаты — перепроверить (могли ожить после cooldown)
            stmt = stmt.where(WpCredential.cred_status == "transient")
        elif scope == "stale":
            stmt = stmt.where(
                or_(
                    WpCredential.last_validated_at.is_(None),
                    WpCredential.last_validated_at < threshold,
                )
            )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return
        for cred in rows:
            site = cred.site
            if site is None or site.deleted_at is not None or not site.is_active:
                continue
            yield cred
        after_id = rows[-1].id
        if len(rows) < batch:
            return


async def _count_creds_to_validate(session: AsyncSession, scope: str) -> int:
    stmt = (
        select(func.count(WpCredential.id))
        .join(WpSite, WpSite.id == WpCredential.site_id)
        .where(
            WpCredential.deleted_at.is_(None),
            WpSite.deleted_at.is_(None),
            WpSite.is_active.is_(True),
        )
    )
    if scope == "invalid":
        stmt = stmt.where(WpCredential.is_valid.is_(False))
    elif scope == "transient":
        stmt = stmt.where(WpCredential.cred_status == "transient")
    elif scope == "stale":
        threshold = datetime.now(UTC) - timedelta(hours=STALE_HOURS)
        stmt = stmt.where(
            or_(
                WpCredential.last_validated_at.is_(None),
                WpCredential.last_validated_at < threshold,
            )
        )
    return int((await session.execute(stmt)).scalar_one())


async def run_validation(scope: str = "all", actor_id: int | None = None) -> dict:
    """
    Главная точка входа для TaskIQ task. Защищён от повторного запуска через
    Redis lock (SET NX). Если уже идёт — возвращает текущий state.
    """
    assert scope in ("all", "invalid", "transient", "stale")

    # Проверяем lock. TTL — только crash-backstop: при нормальном завершении
    # lock снимается в finally. full-валидация (Tier1+Tier2+браузер) на большом
    # scope идёт часами, поэтому TTL щедрый — иначе истёк бы на ходу и позволил
    # второй параллельный прогон. (В UI повторный запуск и так блокирует
    # state.running, но lock — вторая линия обороны.)
    rc = _get_state_client()
    locked = await rc.set("wp_validation:lock", "1", nx=True, ex=8 * 3600)
    if not locked:
        log.info("wp_validation.already_running")
        return (await get_state()).__dict__

    state = ValidationState(
        running=True,
        scope=scope,
        started_at=datetime.now(UTC).isoformat(),
        last_actor_id=actor_id,
    )
    try:
        async with WriteSession() as s:
            state.total = await _count_creds_to_validate(s, scope)
        await _save_state(state)

        if state.total == 0:
            log.info("wp_validation.nothing_to_validate", scope=scope)
            return state.__dict__

        # ── Прокси-пул + клиенты (как в батч-валидаторе) ─────────────────
        # Tier 2 admin-login с серверного IP легко ловит баны, поэтому оба tier-а
        # идут через пул residential-прокси с ротацией по кредам.
        from domain.app_settings.service import get_app_settings
        from domain.proxies.service import proxy_url as _proxy_url_of
        from domain.wp_batches.service import (
            _DomainRateLimiter,
            _build_http_client_url,
        )

        async with WriteSession() as s_cfg:
            _now0 = datetime.now(UTC)
            proxy_pool = list((await s_cfg.execute(
                select(Proxy).where(
                    Proxy.is_active.is_(True),
                    (Proxy.locked_until.is_(None)) | (Proxy.locked_until <= _now0),
                ).order_by(func.random()).limit(_MAX_PROXY_POOL)
            )).scalars().all())
            cf_conc = (await get_app_settings(s_cfg)).cf_browser_concurrency
        # Список proxy-URL для ротации; [None] → прямое соединение (пул пуст).
        proxy_urls: list[str | None] = [_proxy_url_of(p) for p in proxy_pool] or [None]

        rate_limiter = _DomainRateLimiter()
        sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
        # Кэш httpx-клиентов по proxy-URL (один клиент на прокси, переиспользуем).
        _client_cache: dict[str | None, httpx.AsyncClient] = {}

        async def _get_client(purl: str | None) -> httpx.AsyncClient:
            if purl not in _client_cache:
                _client_cache[purl] = await _build_http_client_url(purl)
            return _client_cache[purl]

        async def _validate_one(cred: WpCredential) -> None:
            """Full-цикл одного cred: Tier1 (XML-RPC + CF-браузер) → apply →
            Tier2 (admin-login). Пишет вердикт в state-счётчики."""
            purl = random.choice(proxy_urls)
            http = await _get_client(purl)
            pw = decrypt_password(cred.password)
            if cred.site is not None:
                await rate_limiter.acquire(cred.site.domain)

            # Tier 1 — XML-RPC (+ Patchright для настоящего CF-челленджа)
            poster = XmlRpcPoster(http, timeout_seconds=VALIDATE_TIMEOUT_S, proxy_url=purl)
            try:
                outcome = await poster.validate(
                    site=cred.site, login=cred.login, password=pw,
                )
                if outcome.error == ErrorKind.CF_CHALLENGE:
                    if await _cf_browser_login(cred, pw, purl, cf_conc):
                        outcome = ValidateOutcome(
                            error=ErrorKind.OK, valid_via="admin_browser", role=outcome.role,
                        )
            except Exception as e:  # noqa: BLE001
                log.exception("pool_validate.tier1.unexpected", cred_id=cred.id, error=str(e))
                outcome = ValidateOutcome(error=ErrorKind.NETWORK, error_message=str(e)[:200])

            # Применяем Tier 1 (пишет is_valid + rpc-канал)
            async with WriteSession() as s2:
                fresh = await s2.scalar(
                    select(WpCredential)
                    .where(WpCredential.id == cred.id)
                    .options(selectinload(WpCredential.site))
                )
                if fresh is None:
                    return
                _changed, new_valid, transient = await _apply_validation_result(
                    s2, fresh, outcome
                )

            # Tier 2 — admin form-login (растит admin-канал; может переопределить
            # вердикт: transient→valid при рабочем админе, или →invalid).
            t2 = await _tier2_admin_login(
                fresh, outcome, http, purl, rate_limiter, cf_conc,
            )
            if t2 == "valid":
                new_valid, transient = True, False
            elif t2 == "invalid":
                new_valid, transient = False, False

            # Стейт-инкременты (итоговый вердикт после обоих tier-ов)
            state.done += 1
            if transient:
                state.transient_errors += 1
            elif new_valid:
                state.valid += 1
            else:
                state.invalid += 1
            if state.done % 10 == 0 or state.done == state.total:
                await _save_state(state)

        async def _one(cred: WpCredential) -> None:
            async with sem:
                # Жёсткий per-cred таймаут: один зависший cred не должен держать
                # слот навсегда (Tier2/браузер могут залипнуть на дохлом прокси).
                try:
                    await asyncio.wait_for(
                        _validate_one(cred), timeout=VALIDATE_PER_CRED_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    log.warning("pool_validate.cred_timeout", cred_id=cred.id)
                    state.done += 1
                    state.transient_errors += 1
                except Exception as e:  # noqa: BLE001
                    log.exception("pool_validate.one.failed", cred_id=cred.id, error=str(e))
                    state.done += 1
                    state.transient_errors += 1

        try:
            # Загружаем все creds и пускаем gather
            cred_list: list[WpCredential] = []
            async with WriteSession() as s_iter:
                async for cred in _iter_creds_to_validate(s_iter, scope):
                    cred_list.append(cred)

            await asyncio.gather(*(_one(c) for c in cred_list), return_exceptions=False)
        finally:
            # Закрываем все httpx-клиенты из кэша (по одному на прокси).
            for _http_c in _client_cache.values():
                try:
                    await _http_c.aclose()
                except Exception:
                    pass

        state.finished_at = datetime.now(UTC).isoformat()
        state.running = False
        await _save_state(state)
        log.info(
            "wp_validation.done",
            scope=scope,
            total=state.total,
            valid=state.valid,
            invalid=state.invalid,
            transient=state.transient_errors,
        )
        return state.__dict__
    finally:
        # Снимаем lock и если что-то упало — выставим running=false
        try:
            await rc.delete("wp_validation:lock")
        except Exception:
            pass
        if state.running:
            state.running = False
            state.finished_at = datetime.now(UTC).isoformat()
            await _save_state(state)
