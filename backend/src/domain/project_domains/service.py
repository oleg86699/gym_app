"""Домены проекта + резолв needs_review-задач + аналитика по доменам (Фаза A)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import delete as sa_delete
from sqlalchemy import distinct, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domain.text_links import normalize_domain
from infrastructure.db.models import (
    PostingRun,
    PostingRunStatus,
    ProjectDomain,
    TextItem,
    TextItemStatus,
    WpCredential,
    WpImportBatch,
    WpSite,
)

log = structlog.get_logger(__name__)

# Статусы run-а, из которых после резолва задачу надо пере-дёрнуть (run уже не
# крутится сам). running/queued/scheduled/unpacking — НЕ трогаем (подберут сами).
_REDISPATCH_FROM = {
    PostingRunStatus.NEEDS_REVIEW.value,
    PostingRunStatus.DONE.value,
    PostingRunStatus.FAILED.value,
    PostingRunStatus.INTERRUPTED.value,
    PostingRunStatus.NEED_MORE_ADMINS.value,
    PostingRunStatus.CANCELLED.value,
}


async def list_domains(
    session: AsyncSession, project_id: int, *, include_deleted: bool = False,
) -> list[ProjectDomain]:
    """Домены проекта. include_deleted (только super_admin) — показать и soft-deleted."""
    stmt = (select(ProjectDomain).where(ProjectDomain.project_id == project_id)
            .options(selectinload(ProjectDomain.deleted_by_user)))
    if not include_deleted:
        stmt = stmt.where(ProjectDomain.deleted_at.is_(None))
    return list((await session.scalars(stmt.order_by(ProjectDomain.domain))).all())


async def _redispatch_runs(run_ids: set[int]) -> None:
    """Пере-дёрнуть терминальные/needs_review runs, чтобы они допостили
    дозаполненные задачи."""
    if not run_ids:
        return
    from core.celery_app import celery_app
    from core.db import WriteSession
    async with WriteSession() as s:
        rows = (await s.execute(
            select(PostingRun.id, PostingRun.status, PostingRun.priority)
            .where(PostingRun.id.in_(run_ids), PostingRun.deleted_at.is_(None))
        )).all()
        to_dispatch = [(int(r[0]), str(r[2] or "normal")) for r in rows
                       if str(r[1]) in _REDISPATCH_FROM]
        if not to_dispatch:
            return
        await s.execute(
            update(PostingRun)
            .where(PostingRun.id.in_([rid for rid, _ in to_dispatch]))
            .values(status=PostingRunStatus.QUEUED.value, finished_at=None)
        )
        await s.commit()
    from infrastructure.db.models import CELERY_PRIORITY_MAP
    for rid, prio in to_dispatch:
        try:
            celery_app.send_task("postings.run_posting", args=[rid],
                                 priority=CELERY_PRIORITY_MAP.get(prio, 5))
            log.info("project_domains.redispatch", run_id=rid)
        except Exception as e:
            log.warning("project_domains.redispatch_failed", run_id=rid, error=str(e))


def _pick_candidate_for_domains(candidates, pset: set[str]):
    """Из link_candidates выбрать первую ссылку с доменом ∈ pset."""
    for c in candidates or []:
        if isinstance(c, dict) and c.get("domain") and c["domain"] in pset:
            return c
    return None


async def _auto_resolve(session: AsyncSession, project_id: int) -> set[int]:
    """Пере-сканировать needs_review-задачи проекта: у кого среди кандидатов
    появился домен проекта — авто-резолвим (ставим target + pending).
    Возвращает set затронутых run_id (для пере-дёрга)."""
    pset = {d for d in (
        normalize_domain(x) for x in (await session.scalars(
            select(ProjectDomain.domain).where(
                ProjectDomain.project_id == project_id,
                ProjectDomain.deleted_at.is_(None))
        )).all()
    ) if d}
    if not pset:
        return set()
    items = list((await session.scalars(
        select(TextItem).where(
            TextItem.project_id == project_id,
            TextItem.status == TextItemStatus.NEEDS_REVIEW.value,
        )
    )).all())
    touched_runs: set[int] = set()
    for it in items:
        cand = _pick_candidate_for_domains(it.link_candidates, pset)
        if not cand:
            continue
        it.link_url = cand["link"]
        it.link_anchor = (cand.get("anchor") or None)
        it.target_domain = cand["domain"]
        it.status = TextItemStatus.PENDING.value
        touched_runs.add(it.posting_run_id)
    if touched_runs:
        await session.commit()
    return touched_runs


async def add_domain(session: AsyncSession, project_id: int, domain: str) -> dict:
    """Добавить домен проекту (нормализуем, дедуп). Затем авто-резолв
    needs_review-задач, у кого этот домен появился среди кандидатов."""
    nd = normalize_domain(domain)
    if not nd:
        raise ValueError(f"invalid domain: {domain!r}")
    active = await session.scalar(
        select(ProjectDomain).where(
            ProjectDomain.project_id == project_id, ProjectDomain.domain == nd,
            ProjectDomain.deleted_at.is_(None))
    )
    created = False
    if active is None:
        # если домен был soft-deleted — восстанавливаем ту же запись, не плодим дубль
        soft = await session.scalar(
            select(ProjectDomain).where(
                ProjectDomain.project_id == project_id, ProjectDomain.domain == nd,
                ProjectDomain.deleted_at.is_not(None)).order_by(ProjectDomain.id.desc())
        )
        if soft is not None:
            soft.deleted_at = None
            soft.deleted_by = None
        else:
            session.add(ProjectDomain(project_id=project_id, domain=nd))
        await session.commit()
        created = True
    resolved_runs = await _auto_resolve(session, project_id)
    await _redispatch_runs(resolved_runs)
    return {"domain": nd, "created": created, "auto_resolved_runs": len(resolved_runs)}


async def add_domains(session: AsyncSession, project_id: int, domains: list[str]) -> dict:
    """Добавить СПИСОК доменов разом (по одному на строку / через запятую).
    Нормализуем, дедупим, вставляем новые, затем ОДИН авто-резолв на всё."""
    rows = list((await session.scalars(
        select(ProjectDomain).where(ProjectDomain.project_id == project_id)
    )).all())
    active = {r.domain for r in rows if r.deleted_at is None}
    soft_by_domain = {r.domain: r for r in rows if r.deleted_at is not None}
    added: list[str] = []
    duplicates: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        nd = normalize_domain(raw)
        if not nd:
            if raw.strip():
                invalid.append(raw.strip()[:255])
            continue
        if nd in active or nd in seen:
            duplicates.append(nd)
            continue
        seen.add(nd)
        soft = soft_by_domain.get(nd)
        if soft is not None:  # был soft-deleted — восстанавливаем
            soft.deleted_at = None
            soft.deleted_by = None
        else:
            session.add(ProjectDomain(project_id=project_id, domain=nd))
        added.append(nd)
    if added:
        await session.commit()
    resolved_runs = await _auto_resolve(session, project_id) if added else set()
    await _redispatch_runs(resolved_runs)
    return {
        "added": added,
        "duplicates": duplicates,
        "invalid": invalid,
        "auto_resolved_runs": len(resolved_runs),
    }


async def autobind_link_domains(
    session: AsyncSession, project_id: int, links
) -> int:
    """Safety-net «забыл добавить»: привязать домены ЯВНЫХ целевых ссылок задачи
    (кампания/link-ран/csv-direct) к проекту. Идемпотентно (add_domains дедупит)
    и попутно триггерит авто-резолв уже загруженных needs_review-задач проекта.

    Best-effort: любая ошибка логируется и НЕ роняет создание прогона.
    Возвращает число реально добавленных доменов.
    """
    seen: set[str] = set()
    domains: list[str] = []
    for raw in links or []:
        nd = normalize_domain(raw or "")
        if nd and nd not in seen:
            seen.add(nd)
            domains.append(nd)
    if not domains:
        return 0
    try:
        res = await add_domains(session, project_id, domains)
        added = res.get("added") or []
        if added:
            log.info("project_domains.autobind", project_id=project_id, added=added)
        return len(added)
    except Exception as e:
        log.warning("project_domains.autobind_failed", project_id=project_id, error=str(e))
        return 0


async def remove_domain(
    session: AsyncSession, project_id: int, domain_id: int, *, actor_id: int | None = None,
) -> bool:
    """Soft-delete money-домена: скрываем из списков (super_admin видит с
    include_deleted). Полное удаление из БД — purge_domain (только super_admin)."""
    row = await session.scalar(
        select(ProjectDomain).where(
            ProjectDomain.id == domain_id, ProjectDomain.project_id == project_id,
            ProjectDomain.deleted_at.is_(None))
    )
    if row is None:
        return False
    row.deleted_at = datetime.now(UTC)
    row.deleted_by = actor_id
    await session.commit()
    return True


async def restore_domain(session: AsyncSession, project_id: int, domain_id: int) -> bool:
    """super_admin: вернуть soft-deleted money-домен обратно в проект."""
    row = await session.scalar(
        select(ProjectDomain).where(
            ProjectDomain.id == domain_id, ProjectDomain.project_id == project_id)
    )
    if row is None:
        return False
    row.deleted_at = None
    row.deleted_by = None
    await session.commit()
    return True


async def purge_domain(session: AsyncSession, project_id: int, domain_id: int) -> bool:
    """super_admin: полное (hard) удаление money-домена из БД."""
    res = await session.execute(
        sa_delete(ProjectDomain).where(
            ProjectDomain.id == domain_id, ProjectDomain.project_id == project_id)
    )
    await session.commit()
    return (res.rowcount or 0) > 0


async def resolve_item(
    session: AsyncSession, item_id: int, *, link: str, anchor: str | None
) -> dict:
    """Дозаполнить needs_review-задачу вручную: задать целевую ссылку+анкор,
    перевести в pending, пере-дёрнуть run."""
    it = await session.scalar(select(TextItem).where(TextItem.id == item_id))
    if it is None:
        raise ValueError("item not found")
    nd = normalize_domain(link)
    if not nd:
        raise ValueError(f"invalid link: {link!r}")
    it.link_url = link
    it.link_anchor = (anchor or None) and anchor[:500]
    it.target_domain = nd
    it.status = TextItemStatus.PENDING.value
    run_id = it.posting_run_id
    await session.commit()
    await _redispatch_runs({run_id})
    return {"item_id": item_id, "target_domain": nd, "status": "pending"}


async def resolve_run_by_domain(
    session: AsyncSession, run_id: int, domain: str
) -> dict:
    """Массовый резолв: привязать ОДИН домен ко ВСЕМ needs_review-задачам прогона,
    у кого этот домен есть среди кандидатов — каждой её собственная ссылка/анкор
    (из её текста). Домен в проект НЕ добавляем (разовый резолв). Тексты без ссылки
    на этот домен — пропускаем. Возвращает {resolved, skipped, total}."""
    nd = normalize_domain(domain)
    if not nd:
        raise ValueError(f"invalid domain: {domain!r}")
    items = list((await session.scalars(
        select(TextItem).where(
            TextItem.posting_run_id == run_id,
            TextItem.status == TextItemStatus.NEEDS_REVIEW.value,
        )
    )).all())
    resolved = 0
    for it in items:
        cand = _pick_candidate_for_domains(it.link_candidates, {nd})
        if not cand:
            continue
        anchor = cand.get("anchor") or None
        it.link_url = cand["link"]
        it.link_anchor = anchor[:500] if anchor else None
        it.target_domain = cand["domain"]
        it.status = TextItemStatus.PENDING.value
        resolved += 1
    if resolved:
        await session.commit()
        await _redispatch_runs({run_id})
    return {"resolved": resolved, "skipped": len(items) - resolved, "total": len(items)}


async def needs_review_domains(session: AsyncSession, run_id: int) -> list[dict]:
    """Сводка прогона: различные домены среди кандидатов needs_review-задач +
    сколько задач можно резолвнуть каждым доменом (is_project_domain — уже в
    проекте?). После массового резолва/добавления в проект домен исчезает из
    списка — задач с ним в needs_review больше нет."""
    project_id = await session.scalar(
        select(PostingRun.project_id).where(PostingRun.id == run_id))
    pset: set[str] = set()
    if project_id is not None:
        pset = {d for d in (
            normalize_domain(x) for x in (await session.scalars(
                select(ProjectDomain.domain).where(
                ProjectDomain.project_id == project_id,
                ProjectDomain.deleted_at.is_(None))
            )).all()
        ) if d}
    cand_lists = (await session.scalars(
        select(TextItem.link_candidates).where(
            TextItem.posting_run_id == run_id,
            TextItem.status == TextItemStatus.NEEDS_REVIEW.value,
        )
    )).all()
    counts: dict[str, int] = {}
    for cands in cand_lists:
        seen: set[str] = set()
        for c in (cands or []):
            d = c.get("domain") if isinstance(c, dict) else None
            if d and d not in seen:
                seen.add(d)
                counts[d] = counts.get(d, 0) + 1
    out = [
        {"domain": d, "count": n, "is_project_domain": d in pset}
        for d, n in counts.items()
    ]
    out.sort(key=lambda r: (-r["count"], r["domain"]))
    return out


async def domain_analytics(session: AsyncSession, project_id: int) -> list[dict]:
    """Аналитика по целевым доменам проекта: сколько задач/опубликовано на каждый
    target_domain."""
    rows = (await session.execute(
        select(
            TextItem.target_domain,
            func.count(TextItem.id).label("total"),
            func.count(TextItem.id).filter(
                TextItem.status == TextItemStatus.POSTED.value).label("posted"),
        )
        .where(TextItem.project_id == project_id, TextItem.target_domain.isnot(None))
        .group_by(TextItem.target_domain)
        .order_by(func.count(TextItem.id).desc())
    )).all()
    return [{"target_domain": r[0], "total": int(r[1]), "posted": int(r[2])} for r in rows]


async def domain_summary(
    session: AsyncSession, project_id: int, domain: str,
    allowed_tags: list[str] | None = None,
) -> dict:
    """Сводка по одному целевому домену проекта (для страницы домена).

    allowed_tags — tag-access RBAC: None = весь пул; иначе пул сужается до сайтов
    из батчей с этими тегами (столько постов реально сделает ЭТОТ пользователь).
    Пустой список = нет доступа ни к одному тегу → пул 0."""
    nd = normalize_domain(domain) or domain
    where = (TextItem.project_id == project_id, TextItem.target_domain == nd)
    r = (await session.execute(select(
        func.count(TextItem.id),
        func.count(TextItem.id).filter(TextItem.status == TextItemStatus.POSTED.value),
        func.count(TextItem.id).filter(TextItem.status == TextItemStatus.FAILED.value),
        func.count(TextItem.id).filter(TextItem.status == TextItemStatus.SKIPPED.value),
        func.count(TextItem.id).filter(TextItem.status.in_((
            TextItemStatus.PENDING.value, TextItemStatus.POSTING.value,
            TextItemStatus.NEEDS_REVIEW.value))),
        func.count(distinct(TextItem.site_id)),
        func.count(distinct(TextItem.posting_run_id)),
        func.max(TextItem.posted_at),
    ).where(*where))).first()

    # Свободные сайты ПОД ЭТОТ ДОМЕН: постабельный пул сайтов минус те, на которых
    # уже стоит пост со ссылкой на этот домен (text_items.target_domain + posted).
    # Показывает, на сколько ещё постов хватит уникальных сайтов при max_per_site=1.
    # Тот же предикат «постабельного сайта», что у воркера и project-метрики.
    _cred_conds = [
        WpCredential.site_id == WpSite.id,
        WpCredential.deleted_at.is_(None),
        WpCredential.cred_status == "valid",
        or_(
            WpCredential.can_post_via_xmlrpc.is_(True),
            WpCredential.can_post_via_admin.is_(True),
        ),
    ]
    # tag-access RBAC: сузить пул до батчей с разрешёнными смотрящему тегами.
    if allowed_tags is not None:
        _cred_conds.append(WpCredential.import_batch_id.in_(
            select(WpImportBatch.id).where(WpImportBatch.tag.in_(allowed_tags))))
    postable_cred_exists = exists().where(*_cred_conds)
    pool_total = int((await session.execute(
        select(func.count(WpSite.id)).where(
            WpSite.deleted_at.is_(None), WpSite.is_active.is_(True), postable_cred_exists)
    )).scalar_one())
    used_for_domain = (
        select(distinct(TextItem.site_id)).where(
            TextItem.target_domain == nd,
            TextItem.status == TextItemStatus.POSTED.value,
            TextItem.site_id.isnot(None),
        )
    )
    available_sites = int((await session.execute(
        select(func.count(WpSite.id)).where(
            WpSite.deleted_at.is_(None), WpSite.is_active.is_(True), postable_cred_exists,
            ~WpSite.id.in_(used_for_domain))
    )).scalar_one())

    return {
        "domain": nd, "total": int(r[0]), "posted": int(r[1]), "failed": int(r[2]),
        "skipped": int(r[3]), "in_progress": int(r[4]), "sites": int(r[5]),
        "runs": int(r[6]), "last_posted_at": r[7],
        "available_sites": available_sites, "pool_total": pool_total,
    }


async def domain_runs(session: AsyncSession, project_id: int, domain: str) -> list[dict]:
    """Прогоны проекта, которые бьют по этому целевому домену, с домен-скоупнутыми
    счётчиками (total/posted/failed именно по этому домену в каждом ране)."""
    nd = normalize_domain(domain) or domain
    rows = (await session.execute(
        select(
            PostingRun.id, PostingRun.name, PostingRun.status, PostingRun.task_type,
            PostingRun.content_source, PostingRun.content_mode, PostingRun.run_mode,
            PostingRun.scheduled_for, PostingRun.created_at,
            func.count(TextItem.id),
            func.count(TextItem.id).filter(TextItem.status == TextItemStatus.POSTED.value),
            func.count(TextItem.id).filter(TextItem.status == TextItemStatus.FAILED.value),
        )
        .join(TextItem, TextItem.posting_run_id == PostingRun.id)
        .where(TextItem.project_id == project_id, TextItem.target_domain == nd)
        .group_by(PostingRun.id)
        .order_by(PostingRun.created_at.desc())
        .limit(500)
    )).all()
    return [{
        "id": r[0], "name": r[1], "status": r[2], "task_type": r[3],
        "content_source": r[4], "content_mode": r[5], "run_mode": r[6],
        "scheduled_for": r[7], "created_at": r[8],
        "total": int(r[9]), "posted": int(r[10]), "failed": int(r[11]),
    } for r in rows]


async def domain_placements(session: AsyncSession, project_id: int, domain: str) -> list[dict]:
    """Все проставленные (status=posted) ссылки на домен: где стоит (posted_url),
    на какую ссылку (link_url), анкор, тип (post/sitewide_link/homepage_link — из
    task_type прогона), отметка верификации. Фронт группирует по анкору/ссылке —
    видно, какой анкор/линк ещё нужно «дожать» новыми ссылками."""
    nd = normalize_domain(domain) or domain
    rows = (await session.execute(
        select(
            TextItem.posted_url,
            TextItem.link_url,
            TextItem.link_anchor,
            TextItem.link_verified,
            TextItem.posted_at,
            PostingRun.task_type,
        )
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .where(
            TextItem.project_id == project_id,
            TextItem.target_domain == nd,
            TextItem.status == TextItemStatus.POSTED.value,
        )
        .order_by(TextItem.posted_at.desc())
        .limit(5000)
    )).all()
    return [{
        "posted_url": r[0],
        "link_url": r[1],
        "anchor": r[2] or "",
        "verified": r[3],
        "posted_at": r[4].isoformat() if r[4] else None,
        "type": r[5],
    } for r in rows]


async def domain_items(
    session: AsyncSession, project_id: int, domain: str, *,
    after_id: int | None = None, status: str | None = None, limit: int = 50,
) -> list[dict]:
    """Задачи (text_items) по целевому домену в проекте, последние первыми
    (cursor = id, idx по убыванию). Возвращает limit+1 для has_more."""
    nd = normalize_domain(domain) or domain
    limit = max(1, min(limit, 200))
    q = (select(
            TextItem.id, TextItem.status, TextItem.link_url, TextItem.link_anchor,
            TextItem.posted_url, TextItem.posted_at, TextItem.last_error,
            TextItem.posting_run_id, PostingRun.name, WpSite.domain,
        )
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .outerjoin(WpSite, WpSite.id == TextItem.site_id)
        .where(TextItem.project_id == project_id, TextItem.target_domain == nd))
    if status:
        q = q.where(TextItem.status == status)
    if after_id:
        q = q.where(TextItem.id < after_id)
    q = q.order_by(TextItem.id.desc()).limit(limit + 1)
    rows = (await session.execute(q)).all()
    return [{
        "id": r[0], "status": r[1], "link_url": r[2], "link_anchor": r[3],
        "posted_url": r[4], "posted_at": r[5], "last_error": r[6],
        "run_id": r[7], "run_name": r[8], "site_domain": r[9],
    } for r in rows]
