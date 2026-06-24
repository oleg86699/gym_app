"""Sitewide/homepage link placement — оркестрация поверх WpAdminClient.

Атомарная единица — TextItem link-типа (link_url/link_anchor + site_id).
process_link_item:
  гейт (валидный administrator, edit_theme_options) → login → JIT-probe →
  цепочка методов с обязательным verify → запись placed_via/ref/verified.
Идемпотентность: skip, если у сайта уже есть verified-размещение нашей ссылки.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import distinct, exists, func, select, text as sql_text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import decrypt_password
from core.db import WriteSession
from infrastructure.db.models.posting import (
    PostingRun,
    PostingRunStatus,
    RunTaskType,
    TextItem,
    TextItemStatus,
)
from infrastructure.db.models.wp_access import WpCredential, WpSite
from infrastructure.wp_admin_client import (
    AdminLoginKind,
    WpAdminClient,
)

log = structlog.get_logger(__name__)

PLACE_TIMEOUT_S = 45


# ─── Cred selection / idempotency ────────────────────────────────────


async def pick_admin_cred(session: AsyncSession, site_id: int) -> WpCredential | None:
    """Валидный administrator для сайта (edit_theme_options нужен для сквозной/
    homepage). Author/editor не подходят. Confirmed-login — первым."""
    return await session.scalar(
        select(WpCredential)
        .options(selectinload(WpCredential.site))
        .where(
            WpCredential.site_id == site_id,
            WpCredential.deleted_at.is_(None),
            WpCredential.admin_role == "administrator",
            WpCredential.cred_status == "valid",
            WpCredential.can_admin_login.isnot(False),
            WpCredential.provisioned.is_(False),  # наши provisioned — author
        )
        .order_by(WpCredential.can_admin_login.is_(True).desc(), WpCredential.id.asc())
    )


async def candidate_link_sites(
    session: AsyncSession, *, exclude_existing: bool = True, limit: int | None = None,
    site_langs: list[str] | None = None, site_tlds: list[str] | None = None,
    site_tags: list[str] | None = None, site_domains: list[str] | None = None,
    exclude_site_ids: set[int] | None = None,
) -> list[int]:
    """Сайты-кандидаты под сквозную ссылку: активные, с валидным administrator,
    у кого нашей verified-ссылки ещё нет. Опц. фильтр по language/TLD сайта +
    in-memory exclude (уже занятые/перепробованные в текущем прогоне). Порядок
    случайный — чтобы параллельные айтемы/раны не лезли в одни и те же сайты."""
    from sqlalchemy import or_
    # GROUP BY (не SELECT DISTINCT): иначе PG ругается на ORDER BY random()
    # «must appear in select list». GROUP BY site_id == дедуп по сайту.
    q = (
        select(WpCredential.site_id)
        .join(WpSite, WpSite.id == WpCredential.site_id)
        .where(
            WpCredential.deleted_at.is_(None),
            WpCredential.admin_role == "administrator",
            WpCredential.cred_status == "valid",
            WpCredential.can_admin_login.isnot(False),
            WpCredential.provisioned.is_(False),
            WpSite.deleted_at.is_(None),
            WpSite.is_active.is_(True),
        )
    )
    if site_langs:
        q = q.where(WpSite.language.in_(site_langs))
    if site_tlds:
        q = q.where(or_(*(WpSite.domain.ilike(f"%.{t}") for t in site_tlds)))
    if site_tags:
        q = q.where(or_(*(WpCredential.tags.any(t) for t in site_tags)))
    if site_domains:
        q = q.where(WpSite.domain.in_(site_domains))
    if exclude_site_ids:
        q = q.where(WpCredential.site_id.notin_(exclude_site_ids))
    if exclude_existing:
        already = select(distinct(TextItem.site_id)).where(
            TextItem.placed_via.isnot(None), TextItem.verified_at.isnot(None),
        )
        q = q.where(WpCredential.site_id.notin_(already))
    ids = list((await session.execute(
        q.group_by(WpCredential.site_id).order_by(func.random())
    )).scalars().all())
    return ids[:limit] if limit else ids


async def count_candidate_link_sites(session: AsyncSession) -> int:
    return len(await candidate_link_sites(session))


def _link_count(link: dict) -> int:
    try:
        return max(1, int(float(link.get("count") or 1)))
    except (TypeError, ValueError):
        return 1


async def create_link_run(
    session: AsyncSession, *, project, creator, name: str, task_type: str,
    links: list[dict], concurrency: int, timeout_seconds: int,
    priority: str = "normal", max_sites: int | None = None,
    site_langs: list[str] | None = None, site_tlds: list[str] | None = None,
    site_tags: list[str] | None = None, site_domains: list[str] | None = None,
    site_domains_key: str | None = None,
    max_posts_per_site: int = 1, proxy_selector: str | None = None,
    spread_days: int = 0, scheduled_for: datetime | None = None,
    publish_from: date | None = None, publish_to: date | None = None,
) -> PostingRun:
    """Создать link-run + TextItems.

    count у ссылки = на сколько РАЗНЫХ сайтов её поставить. Сайт НЕ привязываем
    при создании — его выбирает воркер на Start с перебором пула (как постинг):
    крутит доступы пока ссылка не разместится, тогда и пишет site_id/результат.
    max_sites — потолок общего числа размещений.

    proxy_selector — пул прокси (как у постинга). spread_days — drip-feed: размазать
    простановку по N дней (not_before на айтемах). scheduled_for — отложенный старт
    (SCHEDULED → cron поднимет), иначе READY (ручной Start).
    """
    norm = [{"url": (lk.get("url") or lk.get("link") or "").strip(),
             "anchor": (lk.get("anchor") or "").strip(),
             "count": _link_count(lk)} for lk in links]
    norm = [lk for lk in norm if lk["url"]]

    # фильтр пула сохраняем в gen_params — воркер применит при подборе сайтов
    gp: dict = {}
    if site_langs:
        gp["site_langs"] = site_langs
    if site_tlds:
        gp["site_tlds"] = site_tlds
    if site_tags:
        gp["site_tags"] = site_tags
    if site_domains:
        gp["site_domains"] = site_domains
    elif site_domains_key:
        gp["site_domains_key"] = site_domains_key

    status = (PostingRunStatus.SCHEDULED if scheduled_for
              else PostingRunStatus.READY)
    run = PostingRun(
        project_id=project.id, created_by=creator.id, name=name.strip(),
        status=status.value, task_type=task_type,
        concurrency=concurrency, timeout_seconds=timeout_seconds, priority=priority,
        max_posts_per_site=max_posts_per_site, proxy_selector=proxy_selector,
        spread_days=spread_days or 0, scheduled_for=scheduled_for,
        publish_from=publish_from, publish_to=publish_to,
        gen_params=gp or None,
    )
    session.add(run)
    await session.flush()  # нужен run.id

    items: list[TextItem] = []
    seq = 0
    for link in norm:
        url, anchor = link["url"], (link["anchor"] or link["url"])
        # ограничиваем общее число размещений потолком max_sites (если задан)
        for _ in range(link["count"]):
            if max_sites is not None and seq >= max_sites:
                break
            h = hashlib.sha256(f"{url}|{anchor}|{run.id}|{seq}".encode()).hexdigest()
            seq += 1
            items.append(TextItem(
                posting_run_id=run.id, project_id=project.id,
                original_filename="link", title=anchor[:1000],
                content_hash=h, byte_size=len(url.encode()),
                status=TextItemStatus.PENDING.value,
                link_url=url, link_anchor=anchor[:500],  # site_id — на Start
            ))
    session.add_all(items)
    run.total_texts = len(items)
    await session.commit()

    # Drip-feed: размазываем not_before по окну [старт, старт+spread_days].
    # Старт окна = scheduled_for (если отложен) либо now.
    if spread_days and spread_days > 0 and items:
        window_start = scheduled_for or datetime.now(UTC)
        await session.execute(sql_text("""
            UPDATE text_items
            SET not_before = (:ws)::timestamptz
                + (random() * make_interval(secs => :win))
            WHERE posting_run_id = :rid
        """), {"rid": run.id, "ws": window_start, "win": spread_days * 86400})
        await session.commit()

    await session.refresh(run)
    # Авто-привязка доменов целевых ссылок к проекту («забыл добавить» safety-net).
    from domain.project_domains import autobind_link_domains
    await autobind_link_domains(session, project.id, [lk["url"] for lk in norm])
    return run


async def site_has_verified_link(session: AsyncSession, site_id: int) -> bool:
    """Уже стоит наша verified-сквозная на этом сайте? (для skip на повторе)."""
    return bool(await session.scalar(
        select(exists().where(
            TextItem.site_id == site_id,
            TextItem.placed_via.isnot(None),
            TextItem.verified_at.isnot(None),
        ))
    ))


# ─── Place ───────────────────────────────────────────────────────────


async def _place_on_site(item_id: int, site_id: int, task_type: str | None,
                         url: str, anchor: str,
                         proxy_urls: list[str | None] | None = None) -> dict:
    """ОДНА попытка размещения ссылки на конкретном сайте. НЕ помечает айтем
    FAILED при неудаче (это решает перебор в process_link_item) — просто
    возвращает статус. На успех — пишет POSTED + site_id + результат.

    proxy_urls — пул прокси задачи (из proxy_selector); берём случайный per-attempt.
    None/[] → fallback на любой активный прокси (back-compat)."""
    import random

    from domain.proxies.service import pick_active_proxy_url
    from domain.wp_batches.service import _build_http_client_url

    async with WriteSession() as s:
        # идемпотентность — сайт уже с нашей verified-ссылкой
        if await site_has_verified_link(s, site_id):
            return {"ok": True, "status": "skip_exists", "site_id": site_id}
        admin = await pick_admin_cred(s, site_id)
        if admin is None or admin.site is None:
            return {"ok": False, "status": "no_admin", "site_id": site_id}
        site = admin.site
        domain = site.domain
        admin_login = admin.login
        admin_pw = decrypt_password(admin.password)
        admin_id = admin.id
        # клеймим сайт на айтеме (виден в UI как текущая попытка)
        await _mark_item(s, item_id, TextItemStatus.POSTING,
                         credential_id=admin_id, site_id=site_id)
        # Прокси пула задачи (random rotation per attempt); иначе любой активный.
        if proxy_urls:
            _purl = random.choice(proxy_urls)
        else:
            _purl = await pick_active_proxy_url(s)

    http = await _build_http_client_url(_purl)
    try:
        async with http:
            client = WpAdminClient(http, timeout_seconds=PLACE_TIMEOUT_S, proxy_url=_purl)
            lo = await client.login(site=site, login=admin_login, password=admin_pw)
            if lo.error != AdminLoginKind.OK:
                return {"ok": False, "status": f"login_{lo.error.value}", "domain": domain}
            if task_type == RunTaskType.HOMEPAGE_LINK.value:
                outcome = await client.place_homepage_link(site, url, anchor)
            else:
                outcome = await client.place_sitewide_link(site, url, anchor)
    except Exception as e:
        log.warning("link.place.exception", item_id=item_id, error=str(e))
        return {"ok": False, "status": "error", "domain": domain, "error": str(e)[:200]}

    if not outcome.success:
        return {"ok": False, "status": outcome.error.value, "domain": domain,
                "error": outcome.error_message or outcome.error.value}

    now = datetime.now(UTC)
    async with WriteSession() as s:
        await s.execute(update(TextItem).where(TextItem.id == item_id).values(
            status=TextItemStatus.POSTED.value, site_id=site_id, credential_id=admin_id,
            placed_via=outcome.placed_via, placement_ref=outcome.placement_ref,
            verified_at=now, verified_urls=outcome.verified_urls,
            posted_url=(outcome.verified_urls or [None])[0], posted_at=now, last_error=None,
        ))
        await s.commit()
    log.info("link.placed", item_id=item_id, domain=domain, via=outcome.placed_via,
             ref=outcome.placement_ref)
    return {"ok": True, "status": "placed", "domain": domain, "site_id": site_id,
            "placed_via": outcome.placed_via, "placement_ref": outcome.placement_ref,
            "verified_urls": outcome.verified_urls}


async def process_link_item(
    item_id: int, *, used_sites: set[int] | None = None, actor_id: int | None = None,
    proxy_urls: list[str | None] | None = None,
) -> dict:
    """Поставить ссылку для TextItem с ПЕРЕБОРОМ сайтов (как постинг): крутим
    кандидатов из пула, пока не разместим (тогда пишем site_id/результат) или
    пока сайты не кончатся (бюджета нет — терминирует исчерпание пула).

    `used_sites` — общий registry занятых/перепробованных сайтов прогона: атомарно
    клеймим (asyncio single-thread), чтобы параллельные айтемы не целили в один сайт.
    `proxy_urls` — пул прокси задачи (из proxy_selector), прокидываем в _place_on_site.
    """
    if used_sites is None:
        used_sites = set()
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        if item is None:
            return {"ok": False, "status": "not_found", "item_id": item_id}
        run = await s.scalar(
            select(PostingRun).where(PostingRun.id == item.posting_run_id))
        task_type = run.task_type if run else None
        url = item.link_url
        anchor = item.link_anchor or url
        gp = (run.gen_params if run else None) or {}
    if not url:
        async with WriteSession() as s:
            await _mark_item(s, item_id, TextItemStatus.FAILED, last_error="no url")
        return {"ok": False, "status": "error", "item_id": item_id}

    from domain.postings.service import resolve_site_domains
    site_langs = gp.get("site_langs")
    site_tlds = gp.get("site_tlds")
    site_tags = gp.get("site_tags")
    site_domains = resolve_site_domains(gp)
    last_status, last_domain = "no_sites", None
    while True:
        async with WriteSession() as s:
            candidates = await candidate_link_sites(
                s, exclude_existing=True, exclude_site_ids=set(used_sites),
                site_langs=site_langs, site_tlds=site_tlds,
                site_tags=site_tags, site_domains=site_domains, limit=30)
        # синхронный pick + claim: между next() и add() нет await → атомарно
        site_id = next((c for c in candidates if c not in used_sites), None)
        if site_id is None:
            async with WriteSession() as s:
                await _mark_item(s, item_id, TextItemStatus.FAILED,
                                 last_error=f"перебрали все сайты (last: {last_status})")
            return {"ok": False, "status": last_status, "item_id": item_id,
                    "domain": last_domain}
        used_sites.add(site_id)  # claim
        res = await _place_on_site(item_id, site_id, task_type, url, anchor, proxy_urls)
        last_status, last_domain = res.get("status", "error"), res.get("domain")
        if res.get("status") in ("placed", "skip_exists"):
            return res
        # фейл (login/place/verify/no_admin) → следующий сайт; site_id остаётся занят


# ─── Remove ──────────────────────────────────────────────────────────


async def remove_link_item(item_id: int, *, actor_id: int | None = None) -> dict:
    """Снять ранее размещённую сквозную ссылку (по placed_via+placement_ref)."""
    from domain.proxies.service import pick_active_proxy_url
    from domain.wp_batches.service import _build_http_client_url

    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        if item is None or not item.placed_via or not item.placement_ref:
            return {"ok": False, "status": "nothing_to_remove", "item_id": item_id}
        site_id = item.site_id
        placed_via = item.placed_via
        placement_ref = item.placement_ref
        admin = await pick_admin_cred(s, site_id)
        if admin is None or admin.site is None:
            return {"ok": False, "status": "no_admin", "item_id": item_id}
        site = admin.site
        admin_login = admin.login
        admin_pw = decrypt_password(admin.password)
        _purl = await pick_active_proxy_url(s)  # residential exit для curl_cffi-fallback

    http = await _build_http_client_url(_purl)
    removed = False
    try:
        async with http:
            client = WpAdminClient(http, timeout_seconds=PLACE_TIMEOUT_S, proxy_url=_purl)
            lo = await client.login(site=site, login=admin_login, password=admin_pw)
            if lo.error == AdminLoginKind.OK:
                removed = await client.remove_sitewide_link(site, placed_via, placement_ref)
    except Exception as e:
        log.warning("link.remove.exception", item_id=item_id, error=str(e))
        return {"ok": False, "status": "error", "error": str(e)[:200]}

    if removed:
        async with WriteSession() as s:
            await s.execute(update(TextItem).where(TextItem.id == item_id).values(
                status=TextItemStatus.SKIPPED.value,
                placed_via=None, placement_ref=None, verified_at=None,
                verified_urls=None, last_error="removed",
            ))
            await s.commit()
        log.info("link.removed", item_id=item_id, via=placed_via)
        return {"ok": True, "status": "removed", "item_id": item_id}
    return {"ok": False, "status": "remove_failed", "item_id": item_id}


# ─── helpers ─────────────────────────────────────────────────────────


async def _mark_item(session: AsyncSession, item_id: int, status: TextItemStatus,
                     *, last_error: str | None = None, credential_id: int | None = None,
                     site_id: int | None = None):
    vals: dict = {"status": status.value}
    if last_error is not None:
        vals["last_error"] = last_error[:300]
    if credential_id is not None:
        vals["credential_id"] = credential_id
    if site_id is not None:
        vals["site_id"] = site_id
    await session.execute(update(TextItem).where(TextItem.id == item_id).values(**vals))
    await session.commit()
