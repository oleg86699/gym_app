"""Provision-author: создаём СВОЙ WP-аккаунт на сайтах, где у нас есть рабочий
admin-доступ с правом create_users.

Точки входа:
  - provision_site(site_id)        — один сайт (кнопка на /wp-sites)
  - run_batch_provision(batch_id)  — все сайты батча без нашего cred
  - run_bulk_provision()           — все подходящие сайты без нашего cred
  - count_provisionable(...)       — превью «сколько сайтов будет затронуто»

Идемпотентность: сайт, где наш provisioned-cred уже есть, пропускается.
Гейт: нужен valid cred с can_admin_login=True И can_create_users=True.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import distinct, exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import decrypt_password, encrypt_password
from core.db import WriteSession
from infrastructure.db.models.wp_access import WpCredential, WpSite
from infrastructure.wp_admin_client import (
    AdminLoginKind,
    AdminUserCreateKind,
    WpAdminClient,
)

from .identity import generate_identity

log = structlog.get_logger(__name__)

DEFAULT_ROLE = "author"
PROVISION_TAG = "provisioned"
PROVISION_TIMEOUT_S = 45
PROVISION_DEFAULT_CONCURRENCY = 4
_MAX_DUP_RETRIES = 3


# ─── Eligibility queries ─────────────────────────────────────────────


def _eligible_admin_cred_q(site_id: int):
    """Подходящий admin-cred сайта для создания нового пользователя.

    Гейт по can_admin_login — `IS NOT FALSE` (true ИЛИ unknown), а не строго true:
    XML-RPC-валидированные администраторы имеют can_admin_login=NULL (Tier 2 не
    гоняли), но это полноценные админы. Настоящую проверку даёт живой admin-логин
    внутри provision_site. Confirmed-логины ставим первыми в очередь.
    """
    return (
        select(WpCredential)
        .where(
            WpCredential.site_id == site_id,
            WpCredential.deleted_at.is_(None),
            WpCredential.can_admin_login.isnot(False),
            WpCredential.can_create_users.is_(True),
            WpCredential.provisioned.is_(False),
            WpCredential.cred_status == "valid",
        )
        .options(selectinload(WpCredential.site))
        .order_by(WpCredential.can_admin_login.is_(True).desc(), WpCredential.id.asc())
    )


def _has_provisioned_cred_q(site_id: int):
    return select(
        exists().where(
            WpCredential.site_id == site_id,
            WpCredential.provisioned.is_(True),
            WpCredential.deleted_at.is_(None),
        )
    )


async def _target_site_ids(
    session: AsyncSession, *, batch_id: int | None = None
) -> list[int]:
    """Сайты, где есть подходящий admin-cred и ещё НЕТ нашего provisioned-cred."""
    eligible = (
        select(distinct(WpCredential.site_id))
        .where(
            WpCredential.deleted_at.is_(None),
            WpCredential.can_admin_login.isnot(False),  # true ИЛИ unknown
            WpCredential.can_create_users.is_(True),
            WpCredential.provisioned.is_(False),
            WpCredential.cred_status == "valid",
        )
    )
    if batch_id is not None:
        in_batch = select(distinct(WpCredential.site_id)).where(
            WpCredential.import_batch_id == batch_id,
            WpCredential.deleted_at.is_(None),
        )
        eligible = eligible.where(WpCredential.site_id.in_(in_batch))
    # минус сайты, где наш provisioned-cred уже есть
    already = select(distinct(WpCredential.site_id)).where(
        WpCredential.provisioned.is_(True), WpCredential.deleted_at.is_(None)
    )
    q = eligible.where(WpCredential.site_id.notin_(already))
    return list((await session.execute(q)).scalars().all())


async def count_provisionable(
    session: AsyncSession, *, batch_id: int | None = None
) -> int:
    return len(await _target_site_ids(session, batch_id=batch_id))


# ─── Single site ─────────────────────────────────────────────────────


async def provision_site(
    site_id: int,
    *,
    role: str = DEFAULT_ROLE,
    actor_id: int | None = None,
) -> dict:
    """Создать наш аккаунт на одном сайте. Возвращает dict со статусом.

    status: created | skip_exists | no_admin | login_<kind> | create_<kind> | error
    """
    from domain.proxies.service import pick_active_proxy_url
    from domain.wp_batches.service import _build_http_client_url  # избегаем цикла на import-time

    async with WriteSession() as s:
        # уже есть наш cred?
        if await s.scalar(_has_provisioned_cred_q(site_id)):
            return {"ok": True, "site_id": site_id, "status": "skip_exists"}
        admin = await s.scalar(_eligible_admin_cred_q(site_id))
        if admin is None or admin.site is None:
            return {"ok": False, "site_id": site_id, "status": "no_admin"}
        site: WpSite = admin.site
        domain = site.domain
        admin_login = admin.login
        admin_pw = decrypt_password(admin.password)
        admin_id = admin.id
        _purl = await pick_active_proxy_url(s)  # residential exit для curl_cffi-fallback

    # сетевая часть — вне сессии БД
    http = await _build_http_client_url(_purl)
    outcome = None
    used_login = used_email = used_pw = None
    try:
        async with http:
            client = WpAdminClient(http, timeout_seconds=PROVISION_TIMEOUT_S, proxy_url=_purl)
            lo = await client.login(site=site, login=admin_login, password=admin_pw)
            # CF на login → provision этого сайта пропускаем: create_user через
            # браузер-сессию пока не реализован (FlareSolverr убрали, он CF не
            # проходил). Сайт остаётся рабочим для постинга через CF Tier 3
            # (browser-login + curl_cffi replay существующим admin-кредом).
            if lo.error != AdminLoginKind.OK:
                return {"ok": False, "site_id": site_id, "domain": domain,
                        "status": f"login_{lo.error.value}"}
            # создаём пользователя (ретраим на DUPLICATE с новым именем)
            for _ in range(_MAX_DUP_RETRIES):
                used_login, used_email, used_pw = generate_identity(domain)
                outcome = await client.create_user(
                    site, username=used_login, email=used_email,
                    password=used_pw, role=role,
                )
                if outcome.error != AdminUserCreateKind.DUPLICATE:
                    break
    except Exception as e:
        log.warning("provision.exception", site_id=site_id, error=str(e))
        return {"ok": False, "site_id": site_id, "domain": domain, "status": "error",
                "error": str(e)[:200]}

    if outcome is None or not outcome.success:
        kind = outcome.error.value if outcome else "unknown"
        return {"ok": False, "site_id": site_id, "domain": domain,
                "status": f"create_{kind}",
                "error": (outcome.error_message if outcome else None)}

    # успех — пишем новый cred (помечен provisioned)
    now = datetime.now(UTC)
    async with WriteSession() as s:
        stmt = (
            pg_insert(WpCredential)
            .values(
                site_id=site_id,
                login=used_login,
                password=encrypt_password(used_pw),
                tags=[PROVISION_TAG, role],
                note=f"Создан автоматически (provision-{role}) через {outcome.via}",
                provisioned=True,
                provisioned_at=now,
                provisioned_by_cred_id=admin_id,
                provisioned_via=outcome.via,
                admin_role=role,
                can_create_users=(role == "administrator"),
                can_admin_login=True,
                is_valid=True,
                last_validated_at=now,
                # НЕ ставим kind='ok' — это «XML-RPC ответил ok», а наш аккаунт
                # создан и проверен через admin-логин. Валидность держит
                # can_admin_login=True; в summary он корректно считается admin-каналом.
                last_admin_check_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["site_id", "login"],
                index_where=text("deleted_at IS NULL"),
            )
            .returning(WpCredential.id)
        )
        new_id = (await s.execute(stmt)).scalar_one_or_none()
        await s.commit()

        from domain.audit.service import record
        await record(
            s, actor=None, action="wp_credentials.provision",
            resource_type="wp_credential", resource_id=new_id,
            changes={"site_id": site_id, "domain": domain, "login": used_login,
                     "role": role, "via": outcome.via, "user_id": outcome.user_id,
                     "by_cred_id": admin_id, "actor_id": actor_id},
        )
    log.info("provision.created", site_id=site_id, domain=domain, login=used_login,
             role=role, via=outcome.via, wp_user_id=outcome.user_id)
    return {"ok": True, "site_id": site_id, "domain": domain, "status": "created",
            "login": used_login, "role": role, "via": outcome.via,
            "wp_user_id": outcome.user_id, "cred_id": new_id}


# ─── Batch / bulk runners ────────────────────────────────────────────


async def _run_for_sites(
    site_ids: list[int], *, role: str, concurrency: int, actor_id: int | None,
) -> dict:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = []

    async def _one(sid: int):
        async with sem:
            try:
                results.append(await provision_site(sid, role=role, actor_id=actor_id))
            except Exception as e:
                log.warning("provision.site.failed", site_id=sid, error=str(e))
                results.append({"ok": False, "site_id": sid, "status": "error"})

    await asyncio.gather(*[_one(s) for s in site_ids])
    created = sum(1 for r in results if r.get("status") == "created")
    skipped = sum(1 for r in results if r.get("status") == "skip_exists")
    failed = len(results) - created - skipped
    return {"ok": True, "total": len(site_ids), "created": created,
            "skipped": skipped, "failed": failed, "results": results}


async def run_batch_provision(
    batch_id: int, *, role: str = DEFAULT_ROLE,
    concurrency: int = PROVISION_DEFAULT_CONCURRENCY, actor_id: int | None = None,
) -> dict:
    async with WriteSession() as s:
        sites = await _target_site_ids(s, batch_id=batch_id)
    log.info("provision.batch.start", batch_id=batch_id, sites=len(sites), role=role)
    res = await _run_for_sites(sites, role=role, concurrency=concurrency, actor_id=actor_id)
    res["batch_id"] = batch_id
    log.info("provision.batch.done", batch_id=batch_id, **{k: res[k] for k in
             ("total", "created", "skipped", "failed")})
    return res


async def run_bulk_provision(
    *, role: str = DEFAULT_ROLE,
    concurrency: int = PROVISION_DEFAULT_CONCURRENCY, actor_id: int | None = None,
) -> dict:
    async with WriteSession() as s:
        sites = await _target_site_ids(s, batch_id=None)
    log.info("provision.bulk.start", sites=len(sites), role=role)
    res = await _run_for_sites(sites, role=role, concurrency=concurrency, actor_id=actor_id)
    log.info("provision.bulk.done", **{k: res[k] for k in
             ("total", "created", "skipped", "failed")})
    return res
