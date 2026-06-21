"""
Service для WP Sites + WP Credentials.

Сайт — bare-домен (нормализованный). На один сайт может быть много credentials.
Импорт CSV агрегирует строки по нормализованному домену: создаёт/находит site
и под ним добавляет credentials с дедупом по (site_id, login).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import encrypt_password
from infrastructure.db.models import WpCredential, WpSite

_SITE_UNIQ_WHERE = text("deleted_at IS NULL")
_CRED_UNIQ_WHERE = text("deleted_at IS NULL")


@dataclass
class ImportResult:
    imported_credentials: int
    skipped_duplicate_credentials: int
    skipped_invalid_rows: int
    total_rows: int
    sites_created: int
    sites_touched: int


# ─── Domain normalization ─────────────────────────────────────────────


def _clean_domain(d: str) -> str:
    """Bare-домен: lowercase, без www., без path, без port, без протокола/query/fragment."""
    from urllib.parse import urlparse

    d = d.strip()
    if not d:
        return d
    work = d if d.lower().startswith(("http://", "https://")) else "http://" + d

    try:
        parsed = urlparse(work)
    except ValueError:
        return d

    host = (parsed.hostname or "").lower()
    if not host:
        return d
    if host.startswith("www."):
        host = host[4:]
    return host


# ─── WpSite ───────────────────────────────────────────────────────────


async def list_sites(
    session: AsyncSession,
    *,
    after_id: int | None = None,
    limit: int = 50,
    search: str | None = None,
    # all | active | auto-disabled | off  (site-domain status)
    # usable | unusable                   (operational verdict)
    # cred_valid | cred_invalid | cred_transient  (есть ≥1 cred такого статуса)
    status: str = "all",
    sort: str = "alpha",   # alpha | recent | valid_desc | transient_desc
) -> tuple[list[WpSite], int]:
    from sqlalchemy import exists

    base = (
        select(WpSite)
        .where(WpSite.deleted_at.is_(None))
        .options(selectinload(WpSite.credentials))
    )
    if search:
        base = base.where(WpSite.domain.ilike(f"%{search.strip()}%"))

    def _has_cred(cred_status: str):
        return exists().where(
            WpCredential.site_id == WpSite.id,
            WpCredential.deleted_at.is_(None),
            WpCredential.cred_status == cred_status,
        )

    if status == "active":
        base = base.where(WpSite.is_active.is_(True))
    elif status == "auto-disabled":
        base = base.where(WpSite.is_active.is_(False), WpSite.auto_disabled_at.is_not(None))
    elif status == "off":
        base = base.where(WpSite.is_active.is_(False), WpSite.auto_disabled_at.is_(None))
    elif status == "usable":
        # домен жив + есть рабочий cred
        base = base.where(WpSite.is_active.is_(True), _has_cred("valid"))
    elif status == "unusable":
        # домен off ИЛИ нет ни одного valid cred
        base = base.where(or_(WpSite.is_active.is_(False), ~_has_cred("valid")))
    elif status == "cred_valid":
        base = base.where(_has_cred("valid"))
    elif status == "cred_invalid":
        base = base.where(_has_cred("invalid"))
    elif status == "cred_transient":
        base = base.where(_has_cred("transient"))
    # else "all" — no filter

    total = (
        await session.execute(
            select(func.count()).select_from(base.with_only_columns(WpSite.id).subquery())
        )
    ).scalar_one()

    # Sort. Для valid_desc / transient_desc нам нужны агрегаты по credentials —
    # делаем подзапрос с join на credentials. cursor-pagination с after_id
    # работает только в alpha-режиме (id-based); для остальных fallback на
    # offset — но т.к. total сайтов небольшой (десятки), это OK.
    if sort == "recent":
        # max(c.last_validated_at) per site, nulls last
        sub = (
            select(
                WpCredential.site_id,
                func.max(WpCredential.last_validated_at).label("last_check"),
            )
            .where(WpCredential.deleted_at.is_(None))
            .group_by(WpCredential.site_id)
            .subquery()
        )
        page_stmt = (
            base.outerjoin(sub, sub.c.site_id == WpSite.id)
            .order_by(sub.c.last_check.desc().nulls_last(), WpSite.id.asc())
            .limit(limit + 1)
        )
        if after_id:
            page_stmt = page_stmt.offset(after_id)  # «after_id» здесь = offset
    elif sort == "most_used":
        # sum(amount_use) per site, nulls last
        sub = (
            select(
                WpCredential.site_id,
                func.coalesce(func.sum(WpCredential.amount_use), 0).label("uses"),
            )
            .where(WpCredential.deleted_at.is_(None))
            .group_by(WpCredential.site_id)
            .subquery()
        )
        page_stmt = (
            base.outerjoin(sub, sub.c.site_id == WpSite.id)
            .order_by(sub.c.uses.desc().nulls_last(), WpSite.id.asc())
            .limit(limit + 1)
        )
        if after_id:
            page_stmt = page_stmt.offset(after_id)
    elif sort in ("valid_desc", "transient_desc"):
        # Подсчёт valid / transient прямо из generated column cred_status.
        agg = (
            select(
                WpCredential.site_id,
                func.count().filter(WpCredential.cred_status == "valid").label("v"),
                func.count().filter(WpCredential.cred_status == "transient").label("t"),
            )
            .where(WpCredential.deleted_at.is_(None))
            .group_by(WpCredential.site_id)
            .subquery()
        )
        order = agg.c.v.desc() if sort == "valid_desc" else agg.c.t.desc()
        page_stmt = (
            base.outerjoin(agg, agg.c.site_id == WpSite.id)
            .order_by(order.nulls_last(), WpSite.id.asc())
            .limit(limit + 1)
        )
        if after_id:
            page_stmt = page_stmt.offset(after_id)
    else:
        # alpha (default) — стабильно по id, поддерживает cursor-пагинацию
        page_stmt = base.order_by(WpSite.id.asc()).limit(limit + 1)
        if after_id:
            page_stmt = page_stmt.where(WpSite.id > after_id)

    rows = list((await session.execute(page_stmt)).unique().scalars().all())
    return rows, total


async def get_site(session: AsyncSession, site_id: int) -> WpSite | None:
    stmt = (
        select(WpSite)
        .where(WpSite.id == site_id, WpSite.deleted_at.is_(None))
        .options(selectinload(WpSite.credentials))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_site(
    session: AsyncSession,
    *,
    domain: str,
    hint_path: str | None = None,
    hint_port: int | None = None,
    note: str | None = None,
) -> WpSite:
    cleaned = _clean_domain(domain)
    if not cleaned:
        raise ValueError("invalid domain")
    site = WpSite(
        domain=cleaned,
        hint_path=hint_path,
        hint_port=hint_port,
        note=note,
        is_active=True,
    )
    session.add(site)
    await session.commit()
    refreshed = await get_site(session, site.id)
    assert refreshed is not None
    return refreshed


async def update_site(
    session: AsyncSession,
    *,
    site: WpSite,
    domain: str | None = None,
    hint_path: str | None = ...,  # type: ignore[assignment]
    hint_port: int | None = ...,  # type: ignore[assignment]
    is_active: bool | None = None,
    note: str | None = ...,  # type: ignore[assignment]
) -> WpSite:
    if domain is not None:
        site.domain = _clean_domain(domain)
    if hint_path is not ...:  # type: ignore[comparison-overlap]
        site.hint_path = hint_path or None  # type: ignore[assignment]
    if hint_port is not ...:  # type: ignore[comparison-overlap]
        site.hint_port = hint_port  # type: ignore[assignment]
    if is_active is not None:
        site.is_active = is_active
    if note is not ...:  # type: ignore[comparison-overlap]
        site.note = note  # type: ignore[assignment]
    await session.commit()
    refreshed = await get_site(session, site.id)
    assert refreshed is not None
    return refreshed


async def soft_delete_site(session: AsyncSession, site: WpSite) -> None:
    site.deleted_at = datetime.now(UTC)
    for c in site.credentials:
        if c.deleted_at is None:
            c.deleted_at = datetime.now(UTC)
    await session.commit()


# ─── WpCredential ─────────────────────────────────────────────────────


async def list_credentials(
    session: AsyncSession,
    *,
    site_id: int | None = None,
    after_id: int | None = None,
    limit: int = 100,
    search: str | None = None,
    tag: str | None = None,
    is_valid: bool | None = None,
) -> tuple[list[WpCredential], int]:
    base = (
        select(WpCredential)
        .where(WpCredential.deleted_at.is_(None))
        .options(selectinload(WpCredential.site))
    )
    if site_id is not None:
        base = base.where(WpCredential.site_id == site_id)
    if search:
        like = f"%{search.strip()}%"
        base = base.join(WpSite, WpSite.id == WpCredential.site_id).where(
            or_(WpCredential.login.ilike(like), WpSite.domain.ilike(like))
        )
    if tag:
        # tag matches any element in array (Postgres ANY)
        base = base.where(WpCredential.tags.any(tag.strip()))
    if is_valid is not None:
        base = base.where(WpCredential.is_valid.is_(is_valid))

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    page = base.order_by(WpCredential.id.asc()).limit(limit + 1)
    if after_id:
        page = page.where(WpCredential.id > after_id)

    rows = list((await session.execute(page)).unique().scalars().all())
    return rows, total


async def get_credential(session: AsyncSession, cred_id: int) -> WpCredential | None:
    return (
        await session.execute(
            select(WpCredential)
            .where(WpCredential.id == cred_id, WpCredential.deleted_at.is_(None))
            .options(selectinload(WpCredential.site))
        )
    ).scalar_one_or_none()


async def create_credential(
    session: AsyncSession,
    *,
    site_id: int,
    login: str,
    password: str,
    tags: list[str] | None = None,
    note: str | None = None,
    source_filename: str | None = None,
) -> WpCredential | None:
    clean_tags = (
        [t.strip() for t in tags if t and t.strip()] if tags else None
    )
    stmt = (
        pg_insert(WpCredential)
        .values(
            site_id=site_id,
            login=login.strip(),
            password=encrypt_password(password),
            tags=clean_tags or None,
            note=note,
            source_filename=source_filename,
        )
        .on_conflict_do_nothing(index_elements=["site_id", "login"], index_where=_CRED_UNIQ_WHERE)
        .returning(WpCredential.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()
    await session.commit()
    if new_id is None:
        return None
    return await get_credential(session, new_id)


async def update_credential(
    session: AsyncSession,
    *,
    cred: WpCredential,
    login: str | None = None,
    password: str | None = None,
    tags: list[str] | None = ...,  # type: ignore[assignment]
    note: str | None = ...,  # type: ignore[assignment]
    is_valid: bool | None = None,
) -> WpCredential:
    if login is not None:
        cred.login = login.strip()
    if password is not None and password:
        cred.password = encrypt_password(password)
    if tags is not ...:  # type: ignore[comparison-overlap]
        clean = (
            [t.strip() for t in tags if t and t.strip()] if tags else None  # type: ignore[union-attr]
        )
        cred.tags = clean or None  # type: ignore[assignment]
    if note is not ...:  # type: ignore[comparison-overlap]
        cred.note = note  # type: ignore[assignment]
    if is_valid is not None:
        cred.is_valid = is_valid
        if is_valid:
            cred.error_counter = 0
    await session.commit()
    return cred


async def soft_delete_credential(session: AsyncSession, cred: WpCredential) -> None:
    cred.deleted_at = datetime.now(UTC)
    await session.commit()


async def bulk_soft_delete_credentials(session: AsyncSession, ids: list[int]) -> int:
    if not ids:
        return 0
    from sqlalchemy import update

    result = await session.execute(
        update(WpCredential)
        .where(and_(WpCredential.id.in_(ids), WpCredential.deleted_at.is_(None)))
        .values(deleted_at=datetime.now(UTC))
    )
    await session.commit()
    return result.rowcount or 0


def _cred_filter_predicates(
    *, status: str | None, tag: str | None, source: str | None, search: str | None
) -> list:
    """Собрать WHERE-предикаты для bulk-операций по фильтру. Используется и
    delete-by-filter и count-by-filter (preview перед удалением)."""
    preds = [WpCredential.deleted_at.is_(None)]
    if status in ("valid", "invalid", "transient", "pending"):
        preds.append(WpCredential.cred_status == status)
    if tag:
        preds.append(WpCredential.tags.any(tag))  # ARRAY contains
    if source:
        preds.append(WpCredential.source_filename == source)
    if search:
        like = f"%{search.strip()}%"
        preds.append(
            or_(
                WpCredential.login.ilike(like),
                WpCredential.site.has(WpSite.domain.ilike(like)),
            )
        )
    return preds


async def count_credentials_by_filter(
    session: AsyncSession, *, status=None, tag=None, source=None, search=None
) -> int:
    """Сколько cred попадёт под bulk-операцию — для preview/confirm."""
    preds = _cred_filter_predicates(status=status, tag=tag, source=source, search=search)
    return int((await session.execute(
        select(func.count(WpCredential.id)).where(*preds)
    )).scalar_one())


async def bulk_delete_credentials_by_filter(
    session: AsyncSession, *, status=None, tag=None, source=None, search=None
) -> int:
    """Soft-delete всех cred под фильтром (status/tag/source/search). На 600k
    сайтов выбирать IDs вручную нельзя — удаляем по условию одним UPDATE."""
    from sqlalchemy import update

    preds = _cred_filter_predicates(status=status, tag=tag, source=source, search=search)
    # subquery нужен т.к. .has() в UPDATE напрямую неудобен — собираем ids
    ids_subq = select(WpCredential.id).where(*preds).scalar_subquery()
    result = await session.execute(
        update(WpCredential)
        .where(WpCredential.id.in_(ids_subq))
        .values(deleted_at=datetime.now(UTC))
    )
    await session.commit()
    return result.rowcount or 0


# ─── CSV import ───────────────────────────────────────────────────────


async def import_csv(
    session: AsyncSession,
    *,
    csv_bytes: bytes,
    tag: str | None = None,
    source_filename: str | None = None,
    mark_as_valid: bool = True,
) -> ImportResult:
    """Импорт CSV cred → wp-sites.

    `mark_as_valid=True` (default): cred сразу помечается `is_valid=True,
    last_validation_kind='manual_valid', last_validated_at=now()`. Это
    решение пользователя — он импортирует cred которым доверяет, минуя
    batch validation. Иначе cred попадает в неоднозначное состояние
    `pending` без батча, что путало UI.

    `mark_as_valid=False` — оставляем как было: `is_valid=True` (default
    модели), но без `last_validated_at` — cred показывается как pending.
    """
    text_content = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text_content))

    parsed_rows: list[tuple[str, str, str]] = []
    invalid = 0
    total = 0
    first = True

    for row in reader:
        if not row or all(not c.strip() for c in row):
            continue
        total += 1

        if first:
            first = False
            normalized = [c.strip().lower() for c in row[:3]]
            if normalized == ["domain", "login", "password"]:
                total -= 1
                continue

        if len(row) < 3:
            invalid += 1
            continue
        domain_raw, login, password = row[0].strip(), row[1].strip(), row[2].strip()
        if not domain_raw or not login or not password:
            invalid += 1
            continue

        bare = _clean_domain(domain_raw)
        if not bare:
            invalid += 1
            continue
        parsed_rows.append((bare, login, password))

    if not parsed_rows:
        return ImportResult(
            imported_credentials=0,
            skipped_duplicate_credentials=0,
            skipped_invalid_rows=invalid,
            total_rows=total,
            sites_created=0,
            sites_touched=0,
        )

    unique_domains = sorted({d for d, _, _ in parsed_rows})

    existing_sites = (
        await session.execute(
            select(WpSite).where(WpSite.domain.in_(unique_domains), WpSite.deleted_at.is_(None))
        )
    ).scalars().all()
    domain_to_id = {s.domain: s.id for s in existing_sites}

    missing_domains = [d for d in unique_domains if d not in domain_to_id]
    sites_created = 0
    if missing_domains:
        insert_stmt = (
            pg_insert(WpSite)
            .values([{"domain": d} for d in missing_domains])
            .on_conflict_do_nothing(index_elements=["domain"], index_where=_SITE_UNIQ_WHERE)
            .returning(WpSite.id, WpSite.domain)
        )
        new_rows = (await session.execute(insert_stmt)).all()
        sites_created = len(new_rows)
        for sid, dom in new_rows:
            domain_to_id[dom] = sid

        still_missing = [d for d in missing_domains if d not in domain_to_id]
        if still_missing:
            more = (
                await session.execute(select(WpSite).where(WpSite.domain.in_(still_missing)))
            ).scalars().all()
            for s in more:
                domain_to_id[s.domain] = s.id

    tag_list = [tag.strip()] if (tag and tag.strip()) else None
    now = datetime.now(UTC) if mark_as_valid else None
    cred_rows = [
        {
            "site_id": domain_to_id[d],
            "login": login,
            "password": encrypt_password(password),
            "tags": tag_list,
            "source_filename": source_filename,
            **({"is_valid": True,
                "last_validation_kind": "manual_valid",
                "last_validated_at": now} if mark_as_valid else {}),
        }
        for d, login, password in parsed_rows
        if d in domain_to_id
    ]

    if not cred_rows:
        await session.commit()
        return ImportResult(
            imported_credentials=0,
            skipped_duplicate_credentials=0,
            skipped_invalid_rows=invalid,
            total_rows=total,
            sites_created=sites_created,
            sites_touched=len(unique_domains),
        )

    cred_stmt = (
        pg_insert(WpCredential)
        .values(cred_rows)
        .on_conflict_do_nothing(index_elements=["site_id", "login"], index_where=_CRED_UNIQ_WHERE)
        .returning(WpCredential.id)
    )
    inserted = list((await session.execute(cred_stmt)).scalars().all())
    await session.commit()

    imported = len(inserted)
    return ImportResult(
        imported_credentials=imported,
        skipped_duplicate_credentials=len(cred_rows) - imported,
        skipped_invalid_rows=invalid,
        total_rows=total,
        sites_created=sites_created,
        sites_touched=len(unique_domains),
    )


# ─── Aggregates ───────────────────────────────────────────────────────


# Категоризация cred-а для пула. Считаем «подтверждено валидно» только если:
#  - kind ∈ ('ok','manual_valid')              — современный батч-валидатор
#  - ИЛИ kind IS NULL и last_validated_at NOT NULL — legacy (до миграции 0018);
#    такой cred прошёл валидацию когда-то и остался is_valid=True.
# Это исключает свежеимпортированные «default-valid» creds, которых на самом
# деле никто не проверял.
_CONFIRMED_VALID_KINDS = ("ok", "manual_valid")
_CONFIRMED_INVALID_KINDS = ("auth_invalid", "permission_denied", "manual_invalid")


async def pool_summary(session: AsyncSession) -> dict[str, int]:
    """Live агрегаты пула. Читает MV если свежая (см. refresh_pool_summary_mv),
    иначе считает напрямую. Все cred-категории берутся из generated column
    `cred_status` — единый источник истины (миграция 0025)."""
    sites_total = (
        await session.execute(select(func.count(WpSite.id)).where(WpSite.deleted_at.is_(None)))
    ).scalar_one()
    sites_active = (
        await session.execute(
            select(func.count(WpSite.id)).where(
                WpSite.deleted_at.is_(None), WpSite.is_active.is_(True)
            )
        )
    ).scalar_one()

    # Cred-разбивка прямо из cred_status — никакого пере-вычисления предикатов.
    # valid через какой канал: rpc (Tier 1 ok/manual) vs admin (Tier 2 / legacy)
    valid_rpc_pred = and_(
        WpCredential.cred_status == "valid",
        WpCredential.last_validation_kind.in_(("ok", "manual_valid")),
    )
    valid_admin_pred = and_(
        WpCredential.cred_status == "valid",
        or_(
            WpCredential.last_validation_kind.is_(None),
            ~WpCredential.last_validation_kind.in_(("ok", "manual_valid")),
        ),
    )
    creds = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(WpCredential.cred_status == "valid").label("valid"),
                func.count().filter(valid_rpc_pred).label("valid_rpc"),
                func.count().filter(valid_admin_pred).label("valid_admin"),
                func.count().filter(WpCredential.cred_status == "invalid").label("invalid"),
                func.count().filter(WpCredential.cred_status == "pending").label("pending"),
                func.count().filter(WpCredential.cred_status == "transient").label("transient"),
            ).where(WpCredential.deleted_at.is_(None))
        )
    ).one()

    # Operational verdict site-уровня:
    #   USABLE   = is_active=true И есть ≥1 cred со статусом 'valid'
    #   UNUSABLE = is_active=false (auto-disabled) ИЛИ нет ни одного valid cred
    from sqlalchemy import exists

    valid_cred_exists = exists().where(
        WpCredential.site_id == WpSite.id,
        WpCredential.deleted_at.is_(None),
        WpCredential.cred_status == "valid",
    )
    sites_usable = (await session.execute(
        select(func.count(WpSite.id)).where(
            WpSite.deleted_at.is_(None),
            WpSite.is_active.is_(True),
            valid_cred_exists,
        )
    )).scalar_one()
    sites_unusable = (await session.execute(
        select(func.count(WpSite.id)).where(
            WpSite.deleted_at.is_(None),
            or_(
                WpSite.is_active.is_(False),  # auto-off domain
                ~valid_cred_exists,           # нет рабочих cred
            ),
        )
    )).scalar_one()

    return {
        "sites_total": sites_total,
        "sites_active": sites_active,   # legacy: domain alive only
        "sites_usable": int(sites_usable),
        "sites_unusable": int(sites_unusable),
        "credentials_total": creds.total,
        "credentials_valid": creds.valid,
        "credentials_valid_rpc": creds.valid_rpc,
        "credentials_valid_admin": creds.valid_admin,
        "credentials_invalid": creds.invalid,
        "credentials_pending": creds.pending,
        "credentials_transient": creds.transient,
    }


# Сколько секунд MV считается «свежим». Если старше — fallback на live.
POOL_SUMMARY_MV_TTL_SEC = 90


async def pool_summary_cached(session: AsyncSession) -> dict[str, int]:
    """Читает summary из materialized view (дёшево, <1ms). Если MV устарел
    (>TTL) или пуст — fallback на live `pool_summary`. Используется для
    высокочастотного polling карточек на /wp-sites."""
    from sqlalchemy import text

    try:
        row = (await session.execute(text(
            "SELECT sites_total, sites_active, sites_usable, sites_unusable, "
            "credentials_total, credentials_valid, credentials_valid_rpc, "
            "credentials_valid_admin, credentials_invalid, "
            "credentials_pending, credentials_transient, "
            "EXTRACT(EPOCH FROM (now() - computed_at)) AS age_sec "
            "FROM wp_pool_summary_mv WHERE id = 1"
        ))).one_or_none()
    except Exception as e:
        log.warning("pool_summary_mv.read_failed", error=str(e))
        row = None

    if row is None or (row.age_sec is not None and row.age_sec > POOL_SUMMARY_MV_TTL_SEC):
        # MV пуст или протух — считаем напрямую (и фоновый cron скоро обновит MV)
        return await pool_summary(session)

    return {
        "sites_total": int(row.sites_total),
        "sites_active": int(row.sites_active),
        "sites_usable": int(row.sites_usable),
        "sites_unusable": int(row.sites_unusable),
        "credentials_total": int(row.credentials_total),
        "credentials_valid": int(row.credentials_valid),
        "credentials_valid_rpc": int(row.credentials_valid_rpc),
        "credentials_valid_admin": int(row.credentials_valid_admin),
        "credentials_invalid": int(row.credentials_invalid),
        "credentials_pending": int(row.credentials_pending),
        "credentials_transient": int(row.credentials_transient),
    }


async def refresh_pool_summary_mv(session: AsyncSession) -> None:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY — не блокирует читателей.
    Вызывается из cron (раз в минуту) и сразу после batch validation."""
    from sqlalchemy import text

    try:
        await session.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY wp_pool_summary_mv")
        )
        await session.commit()
    except Exception as e:
        # CONCURRENTLY требует уже наполненный MV; на первом разе делаем обычный.
        log.warning("pool_summary_mv.refresh_concurrent_failed", error=str(e))
        await session.rollback()
        await session.execute(text("REFRESH MATERIALIZED VIEW wp_pool_summary_mv"))
        await session.commit()


async def list_credential_tags(session: AsyncSession) -> list[str]:
    """Уникальные теги (unnest из tags array)."""
    from sqlalchemy import func as _f

    rows = (
        await session.execute(
            select(_f.distinct(_f.unnest(WpCredential.tags)).label("t"))
            .where(WpCredential.deleted_at.is_(None))
            .order_by("t")
        )
    ).all()
    return [r[0] for r in rows if r[0]]


# ─── Site analytics ───────────────────────────────────────────────────


async def site_analytics(
    session: AsyncSession, site_id: int, recent_limit: int = 50
) -> dict:
    """
    Аналитика по WP-сайту:
      - Сколько постов опубликовано всего / 24ч / 7д
      - Самый ранний / самый поздний пост
      - Уникальные проекты использовавшие этот сайт
      - Уникальные cred-ы которые отработали посты
      - Последние N успешных постов с деталями

    Возвращает dict для прямой передачи в Pydantic схему.
    """
    from datetime import UTC, datetime, timedelta

    from infrastructure.db.models import (
        AdminUser,
        PostingRun,
        Project,
        TextItem,
        TextItemStatus,
        WpCredential,
    )

    now = datetime.now(UTC)

    # Aggregate counters одним запросом
    agg = (
        await session.execute(
            select(
                func.count(TextItem.id).label("total"),
                func.count(TextItem.id)
                .filter(TextItem.posted_at >= now - timedelta(hours=24))
                .label("h24"),
                func.count(TextItem.id)
                .filter(TextItem.posted_at >= now - timedelta(days=7))
                .label("d7"),
                func.min(TextItem.posted_at).label("first_at"),
                func.max(TextItem.posted_at).label("last_at"),
                func.count(func.distinct(TextItem.project_id)).label("distinct_proj"),
                func.count(func.distinct(TextItem.credential_id)).label("distinct_cred"),
            ).where(
                TextItem.site_id == site_id,
                TextItem.status == TextItemStatus.POSTED.value,
            )
        )
    ).one()

    # Recent posts с join-ом на run/project/credential
    recent_q = (
        select(
            TextItem.id,
            TextItem.posting_run_id,
            TextItem.title,
            TextItem.posted_url,
            TextItem.posted_at,
            TextItem.credential_id,
            PostingRun.name.label("run_name"),
            PostingRun.created_by.label("run_creator_id"),
            Project.id.label("project_id"),
            Project.name.label("project_name"),
            WpCredential.login.label("cred_login"),
            AdminUser.username.label("creator_username"),
        )
        .join(PostingRun, PostingRun.id == TextItem.posting_run_id)
        .join(Project, Project.id == TextItem.project_id)
        .outerjoin(WpCredential, WpCredential.id == TextItem.credential_id)
        .outerjoin(AdminUser, AdminUser.id == PostingRun.created_by)
        .where(
            TextItem.site_id == site_id,
            TextItem.status == TextItemStatus.POSTED.value,
        )
        .order_by(TextItem.posted_at.desc())
        .limit(recent_limit)
    )
    recent_rows = list((await session.execute(recent_q)).all())

    posts = [
        {
            "text_item_id": int(r.id),
            "posting_run_id": int(r.posting_run_id),
            "run_name": str(r.run_name),
            "run_creator_id": int(r.run_creator_id) if r.run_creator_id is not None else None,
            "run_creator_username": r.creator_username,
            "project_id": int(r.project_id),
            "project_name": str(r.project_name),
            "credential_id": int(r.credential_id) if r.credential_id is not None else None,
            "credential_login": r.cred_login,
            "posted_url": r.posted_url,
            "posted_at": r.posted_at,
            "text_title": r.title,
        }
        for r in recent_rows
    ]

    return {
        "site_id": site_id,
        "posts_total": int(agg.total or 0),
        "posts_24h": int(agg.h24 or 0),
        "posts_7d": int(agg.d7 or 0),
        "first_posted_at": agg.first_at,
        "last_posted_at": agg.last_at,
        "distinct_projects": int(agg.distinct_proj or 0),
        "distinct_credentials_used": int(agg.distinct_cred or 0),
        "recent_posts": posts,
    }
