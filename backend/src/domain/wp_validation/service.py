"""
Валидатор WP-credentials.

Использует XmlRpcPoster.validate (wp.getUsersBlogs) — лёгкий auth-check без
побочек. Состояние массовой валидации хранится в Redis (DB 9), чтобы UI
видел прогресс без БД-нагрузки.

Применяется в:
- TaskIQ scheduled cron: stale-only (раз в 4 часа).
- On-demand TaskIQ task: scope='all' | 'invalid' | 'stale' — из UI кнопки.

После каждой credential:
- ok → is_valid=True, error_counter=0, last_validated_at=now
- AUTH_INVALID/PERMISSION_DENIED → error_counter+=1; при >=INVALIDATE_THRESHOLD → is_valid=False
- NETWORK/SERVER/UNKNOWN → не меняем флаги (сайт временно недоступен); last_validated_at обновляем
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import redis.asyncio as aioredis
import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.crypto import decrypt_password
from core.db import WriteSession
from infrastructure.db.models import WpCredential, WpSite
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
    from sqlalchemy import func

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
    assert scope in ("all", "invalid", "stale")

    # Проверяем lock
    rc = _get_state_client()
    locked = await rc.set("wp_validation:lock", "1", nx=True, ex=30 * 60)
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

        sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=VALIDATE_TIMEOUT_S, follow_redirects=True
        ) as http:
            poster = XmlRpcPoster(http, timeout_seconds=VALIDATE_TIMEOUT_S)

            async def _one(cred: WpCredential) -> None:
                async with sem:
                    try:
                        outcome = await poster.validate(
                            site=cred.site,
                            login=cred.login,
                            password=decrypt_password(cred.password),
                        )
                    except Exception as e:
                        log.exception("wp_validation.one.unexpected", cred_id=cred.id, error=str(e))
                        return
                    async with WriteSession() as s2:
                        # Перечитываем credential свежим (могла измениться error_counter)
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
                    # Стейт-инкременты
                    state.done += 1
                    if transient:
                        state.transient_errors += 1
                    elif new_valid:
                        state.valid += 1
                    else:
                        state.invalid += 1
                    # Сохраняем каждые 10 итераций — чтобы UI видел прогресс
                    if state.done % 10 == 0 or state.done == state.total:
                        await _save_state(state)

            # Загружаем все creds (id-only достаточно) и пускаем gather
            cred_list: list[WpCredential] = []
            async with WriteSession() as s_iter:
                async for cred in _iter_creds_to_validate(s_iter, scope):
                    cred_list.append(cred)

            await asyncio.gather(*(_one(c) for c in cred_list), return_exceptions=False)

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
