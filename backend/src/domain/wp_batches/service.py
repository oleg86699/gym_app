"""
WP Import Batches: создание из CSV + per-batch валидация.

Pipeline:
  1. import_csv → создаёт батч, парсит rows, делает sites_upsert + cred_insert
     с ON CONFLICT DO NOTHING. Новые cred привязаны к этому батчу. Дубликаты
     остаются в пуле под своим прежним батчем.
  2. validate(batch_id, opts) → TaskIQ task, per-credential через XmlRpcPoster:
     - per-domain rate-limit (≥30 сек)
     - concurrency 5 (default)
     - random UA на каждый запрос
     - опц. через proxy
     - language detection inline (один GET на homepage)
     - AUTH-ошибка (403 «Incorrect username or password» и т.п.) — это
       детерминированный ответ WP, ретраить смысла нет. Первая такая
       ошибка → is_valid=False, дальше cred в cooldown на 2ч (cooldown
       нужен только чтобы не считать ту же ошибку при параллельных
       корутинах того же батча).
     - Site-class ошибки (network/server_error/site_not_found/xmlrpc_disabled)
       бьют по сайту, а не по cred. При N подряд site-failures по сайту
       (учитываются все credentials) — сайт авто-выключается (is_active=False).
       Любой успех/auth-fail (т.е. сайт ответил) сбрасывает site-счётчик.
"""

from __future__ import annotations

import asyncio
import csv
import io
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import desc, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.crypto import decrypt_password, encrypt_password
from core.db import WriteSession
from core.lang_detect import detect_language
from core.useragents import random_ua
from domain.wp_sites.service import _clean_domain
from infrastructure.db.models import (
    AdminUser,
    Proxy,
    WpBatchStatus,
    WpCredential,
    WpImportBatch,
    WpSite,
)
from infrastructure.wp_client import (
    DEFINITIVE_CRED_INVALID_KINDS,
    ErrorKind,
    ValidateOutcome,
    XmlRpcPoster,
)

log = structlog.get_logger(__name__)


# ─── Конфиг ──────────────────────────────────────────────────────────

VALIDATE_DEFAULT_CONCURRENCY = 5
# Таймаут на ОДИН HTTP-запрос внутри validate (discovery GET, либо XML-RPC POST).
# Через proxy + slow shared hosting первое подключение к xmlrpc.php реально
# может занимать 20-30 сек → ставим 45 сек с запасом.
VALIDATE_TIMEOUT_S = 45

# Жёсткий потолок на полный validation одного cred (discovery + XML-RPC + lang detect
# + rate-limit wait). Без него зависший httpx-запрос держит слот семафора
# до бесконечности. См. asyncio.wait_for в _one().
VALIDATE_PER_CRED_TIMEOUT_S = 180

DOMAIN_RATE_LIMIT_S = 30        # не дёргать один и тот же домен чаще
COOLDOWN_AFTER_ERROR = timedelta(hours=2)
# AUTH-ошибки детерминированные — первая же бракует cred. БЕЗ cooldown:
# пароль не «починится» за 2 часа, отсроченная повторная проверка лишняя.
INVALIDATE_THRESHOLD = 1
# Рабочая выборка прокси на батч-валидацию (ротация по ней + ретрай дохлых).
# Кап, чтобы кэш httpx-клиентов не разросся до тысяч (по одному на прокси).
_MAX_PROXY_POOL = 64

# Recoverable errors — сайт временно недоступен, может ожить через какое-то
# время. На таких cred ставим cooldown чтобы фоновый revalidator и
# параллельные корутины батча не били зря до окончания cooldown.
_RECOVERABLE_KINDS: set[ErrorKind] = {
    ErrorKind.NETWORK,           # таймаут / connection refused — IP/proxy/site flap
    ErrorKind.SERVER_ERROR,      # 5xx — backend упал, может подняться
    ErrorKind.CF_CHALLENGE,      # CF block — может отпустить
    ErrorKind.RATE_LIMITED,      # явно сказано «too many» — подождать
    ErrorKind.TASK_TIMEOUT,      # жёсткий timeout — peripheral hang
    ErrorKind.CAPTCHA_REQUIRED,  # капча — внешне может смениться/сняться
}

# Сайт-class ошибки (network/server_error/site_not_found/xmlrpc_disabled/task_timeout)
# не бьют по cred, но N подряд по разным cred одного сайта означают
# «домен мёртв» → авто-выключаем site.is_active.
SITE_FAILURE_DISABLE_THRESHOLD = 10

_CRED_UNIQ_WHERE = text("deleted_at IS NULL")


# ─── CRUD ────────────────────────────────────────────────────────────


async def list_batches(
    session: AsyncSession,
    *,
    after_id: int | None = None,
    limit: int = 100,
    owner_id: int | None = None,
) -> list[WpImportBatch]:
    """owner_id — если задан, вернуть ТОЛЬКО батчи этого пользователя
    (owner-scoping для портала поставщика)."""
    stmt = (
        select(WpImportBatch)
        .where(WpImportBatch.deleted_at.is_(None))
        .order_by(desc(WpImportBatch.id))
        .limit(limit + 1)
    )
    if owner_id is not None:
        stmt = stmt.where(WpImportBatch.created_by_user_id == owner_id)
    if after_id:
        stmt = stmt.where(WpImportBatch.id < after_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_batch(session: AsyncSession, batch_id: int) -> WpImportBatch | None:
    return await session.scalar(
        select(WpImportBatch).where(
            WpImportBatch.id == batch_id, WpImportBatch.deleted_at.is_(None)
        )
    )


async def compute_batch_counters(
    session: AsyncSession, batch_ids: list[int]
) -> dict[int, dict[str, int]]:
    """
    Live-счётчики из wp_credentials per batch:
      {batch_id: {valid, invalid, transient, pending, total}}

    Считается из текущего состояния `last_validation_kind`, а не из stored
    counters на батче — чтобы UI видел свежие цифры сразу.
    """
    if not batch_ids:
        return {}
    from sqlalchemy import and_, or_

    xmlrpc_ok_kinds = ("ok", "manual_valid")
    # Базовые категории — прямо из generated column cred_status (единый
    # источник истины, миграция 0025). Дополнительно для valid даём разбивку
    # rpc/admin — её cred_status не несёт (он про итоговый verdict, не канал).
    is_valid_cat = WpCredential.cred_status == "valid"
    valid_xmlrpc_pred = and_(
        is_valid_cat,
        WpCredential.last_validation_kind.is_not(None),
        WpCredential.last_validation_kind.in_(xmlrpc_ok_kinds),
    )
    # admin-valid = всё остальное в valid (kind=NULL legacy или Tier2 admin)
    valid_admin_pred = and_(
        is_valid_cat,
        or_(
            WpCredential.last_validation_kind.is_(None),
            ~WpCredential.last_validation_kind.in_(xmlrpc_ok_kinds),
        ),
    )
    stmt = (
        select(
            WpCredential.import_batch_id,
            func.count(WpCredential.id).filter(is_valid_cat).label("valid"),
            func.count(WpCredential.id).filter(valid_xmlrpc_pred).label("valid_xmlrpc"),
            func.count(WpCredential.id).filter(valid_admin_pred).label("valid_admin"),
            func.count(WpCredential.id).filter(WpCredential.cred_status == "invalid").label("invalid"),
            func.count(WpCredential.id).filter(WpCredential.cred_status == "transient").label("transient"),
            func.count(WpCredential.id).filter(WpCredential.cred_status == "pending").label("pending"),
            func.count(WpCredential.id).label("total"),
        )
        .where(
            WpCredential.import_batch_id.in_(batch_ids),
            WpCredential.deleted_at.is_(None),
        )
        .group_by(WpCredential.import_batch_id)
    )
    rows = (await session.execute(stmt)).all()
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out[int(r[0])] = {
            "valid": int(r[1] or 0),
            "valid_xmlrpc": int(r[2] or 0),
            "valid_admin": int(r[3] or 0),
            "invalid": int(r[4] or 0),
            "transient": int(r[5] or 0),
            "pending": int(r[6] or 0),
            "provisioned": 0,
            "total": int(r[7] or 0),
        }
    for bid in batch_ids:
        out.setdefault(bid, {
            "valid": 0, "valid_xmlrpc": 0, "valid_admin": 0,
            "invalid": 0, "transient": 0, "pending": 0, "provisioned": 0, "total": 0,
        })

    # «наши аккаунты» этого батча: provisioned-креды НЕ лежат в батче (import_batch_id
    # = NULL), но ссылаются на админ-кред батча через provisioned_by_cred_id.
    src = WpCredential.__table__.alias("src")
    prov_rows = (await session.execute(
        select(src.c.import_batch_id, func.count(WpCredential.id))
        .select_from(WpCredential)
        .join(src, WpCredential.provisioned_by_cred_id == src.c.id)
        .where(
            src.c.import_batch_id.in_(batch_ids),
            WpCredential.provisioned.is_(True),
            WpCredential.deleted_at.is_(None),
        )
        .group_by(src.c.import_batch_id)
    )).all()
    for bid, cnt in prov_rows:
        if bid in out:
            out[int(bid)]["provisioned"] = int(cnt or 0)
    return out


async def soft_delete_batch(session: AsyncSession, batch_id: int) -> None:
    """Удаляет ЗАПИСЬ батча. Credentials остаются в пуле (только теряют batch_id)."""
    await session.execute(
        update(WpImportBatch)
        .where(WpImportBatch.id == batch_id)
        .values(deleted_at=datetime.now(UTC))
    )
    await session.execute(
        update(WpCredential)
        .where(WpCredential.import_batch_id == batch_id)
        .values(import_batch_id=None)
    )
    await session.commit()


# ─── Import CSV → batch ──────────────────────────────────────────────


@dataclass
class BatchImportResult:
    batch_id: int
    parsed_rows: int
    sites_created: int
    sites_touched: int
    credentials_new: int
    credentials_duplicate: int
    skipped_invalid_rows: int


def _parse_csv_credentials(text: str) -> tuple[list[tuple[str, str, str]], int]:
    """CSV формат: первая строка header `domain,login,password`. Возвращает
    (parsed_rows, skipped_invalid)."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty")

    header = [c.strip().lower() for c in rows[0]]
    required = {"domain", "login", "password"}
    if not required.issubset(set(header)):
        raise ValueError("CSV must have columns: domain, login, password")
    idx_domain = header.index("domain")
    idx_login = header.index("login")
    idx_password = header.index("password")

    parsed: list[tuple[str, str, str]] = []
    skipped = 0
    for row in rows[1:]:
        if len(row) <= max(idx_domain, idx_login, idx_password):
            skipped += 1
            continue
        domain_raw = row[idx_domain].strip()
        login = row[idx_login].strip()
        password = row[idx_password].strip()
        if not domain_raw or not login or not password:
            skipped += 1
            continue
        bare = _clean_domain(domain_raw)
        if not bare:
            skipped += 1
            continue
        parsed.append((bare, login, password))
    return parsed, skipped


def _parse_txt_credentials(text: str) -> tuple[list[tuple[str, str, str]], int]:
    """
    TXT формат (tab-separated, без header):
        domain<TAB>url<TAB>[number_or_empty]<TAB>login<TAB>password
        domain<TAB>url<TAB>login<TAB>password           # без middle column
        domain<TAB>login<TAB>password                   # минимум

    Логика разбора каждой строки:
      1. split по табам (whitespace-fallback если табов нет)
      2. если поле содержит '://' — это URL, выбрасываем (bare domain у нас есть)
      3. если поле — чисто число — выбрасываем (это та самая «0»-колонка)
      4. остаются: первое = domain, последние два = login + password

    Пустые строки и строки начинающиеся с # — игнорируем.
    """
    parsed: list[tuple[str, str, str]] = []
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Tab-separated предпочтительно; если табов нет — fallback на split по
        # любому whitespace
        fields = line.split("\t") if "\t" in line else line.split()
        fields = [f.strip() for f in fields if f.strip()]
        if len(fields) < 3:
            skipped += 1
            continue

        # Фильтрация: убираем URL и числовые колонки
        clean_fields: list[str] = []
        for f in fields:
            if "://" in f:
                continue   # URL — выбрасываем (bare domain должен быть отдельно)
            if f.lstrip("-").replace(".", "", 1).isdigit():
                continue   # «0», «1.5» и т.п. — выбрасываем
            clean_fields.append(f)

        if len(clean_fields) < 3:
            # После очистки осталось <3 — нечем заполнить (domain, login, password)
            skipped += 1
            continue

        domain_raw = clean_fields[0]
        # login + password = последние два non-trimmed элемента
        # (после первого, который domain)
        login = clean_fields[-2]
        password = clean_fields[-1]

        bare = _clean_domain(domain_raw)
        if not bare or not login or not password:
            skipped += 1
            continue
        parsed.append((bare, login, password))
    return parsed, skipped


def _parse_credentials_bytes(
    file_bytes: bytes, filename: str | None
) -> tuple[list[tuple[str, str, str]], int]:
    """
    Распарсить credential-файл. Формат определяется по расширению:
      - .csv → CSV с header (domain,login,password)
      - .txt → tab-separated без header, см. `_parse_txt_credentials`
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename and "." in filename else "csv"
    if ext == "txt":
        return _parse_txt_credentials(text)
    return _parse_csv_credentials(text)


async def import_csv_as_batch(
    session: AsyncSession,
    *,
    csv_bytes: bytes,
    name: str,
    tag: str | None,
    note: str | None,
    source_filename: str | None,
    cost_total: float | None,
    cost_currency: str | None,
    creator: AdminUser | None,
) -> BatchImportResult:
    """
    Полный pipeline:
      1. parse CSV/TXT (формат выбирается по расширению source_filename)
      2. group rows by bare domain → sites upsert
      3. для credentials — pg_insert.on_conflict_do_nothing
      4. создаём batch row, привязываем все НОВЫЕ credentials
      5. возвращаем счётчики (включая дубликаты)

    NB: параметр всё ещё называется `csv_bytes` для совместимости — принимает
    также TXT (определяется по расширению source_filename).
    """
    parsed_rows, skipped_invalid = _parse_credentials_bytes(csv_bytes, source_filename)
    total_rows = len(parsed_rows)

    # 1. Batch entity
    batch = WpImportBatch(
        name=name.strip(),
        tag=(tag or "").strip() or None,
        note=note,
        cost_total=cost_total,
        cost_currency=(cost_currency or "").strip() or None,
        source_filename=source_filename,
        status=WpBatchStatus.UPLOADED.value,
        created_by_user_id=creator.id if creator else None,
        total_credentials=total_rows,
    )
    session.add(batch)
    await session.flush()

    # 2. Sites upsert
    unique_domains = list({bare for bare, _, _ in parsed_rows})
    sites_created = 0
    sites_touched = len(unique_domains)
    if unique_domains:
        # bulk upsert sites
        existing = (
            await session.execute(select(WpSite).where(WpSite.domain.in_(unique_domains)))
        ).scalars().all()
        existing_domains = {s.domain for s in existing}
        for d in unique_domains:
            if d not in existing_domains:
                session.add(WpSite(domain=d, is_active=True))
                sites_created += 1
        await session.flush()

    # domain → site_id
    sites = (
        await session.execute(select(WpSite).where(WpSite.domain.in_(unique_domains)))
    ).scalars().all()
    domain_to_id = {s.domain: s.id for s in sites}

    # 3. Insert credentials (skip duplicates)
    cred_rows = [
        {
            "site_id": domain_to_id[d],
            "login": login,
            "password": encrypt_password(password),
            "source_filename": source_filename,
            "import_batch_id": batch.id,
        }
        for d, login, password in parsed_rows
        if d in domain_to_id
    ]

    credentials_new = 0
    credentials_duplicate = 0
    duplicate_cred_ids: list[int] = []
    if cred_rows:
        # asyncpg/PG: один INSERT не может иметь >32767 bind-параметров. ВНИМАНИЕ:
        # pg_insert(WpCredential).values([...]) биндит НЕ только 5 ключей из dict, а
        # ВСЕ колонки с python-дефолтами (is_valid/error_counter/amount_use/provisioned/
        # created_at/updated_at/…) — реально ~11 параметров на строку. Поэтому чанк
        # маленький. Явный multi-row .values() НЕ авто-чанкается (в отличие от ORM-flush).
        _CHUNK = 1000  # 1000 × ~11 колонок ≈ 11k параметров — с запасом < 32767
        inserted_ids: set[int] = set()
        for _i in range(0, len(cred_rows), _CHUNK):
            chunk = cred_rows[_i:_i + _CHUNK]
            stmt = (
                pg_insert(WpCredential)
                .values(chunk)
                .on_conflict_do_nothing(
                    index_elements=["site_id", "login"], index_where=_CRED_UNIQ_WHERE
                )
                .returning(WpCredential.id)
            )
            inserted_ids.update((await session.execute(stmt)).scalars().all())
        credentials_new = len(inserted_ids)
        credentials_duplicate = len(cred_rows) - credentials_new

        # Найти «оригиналы» для пропущенных дубликатов (для filter='duplicates' в UI).
        # Тоже чанками (tuple_-IN = 2 параметра на пару); уже вставленные отсеиваем в Python.
        if credentials_duplicate > 0:
            from sqlalchemy import tuple_

            for _i in range(0, len(cred_rows), _CHUNK):
                chunk = cred_rows[_i:_i + _CHUNK]
                target_pairs = [(r["site_id"], r["login"]) for r in chunk]
                rows = await session.execute(
                    select(WpCredential.id).where(
                        tuple_(WpCredential.site_id, WpCredential.login).in_(target_pairs),
                        WpCredential.deleted_at.is_(None),
                    )
                )
                for cid in rows.scalars().all():
                    if cid not in inserted_ids:
                        duplicate_cred_ids.append(int(cid))

    # 4. Counters на батч
    batch.duplicate_credentials = credentials_duplicate
    batch.duplicate_cred_ids = duplicate_cred_ids
    batch.total_credentials = len(cred_rows)  # only valid CSV rows

    await session.commit()

    log.info(
        "wp_batches.import",
        batch_id=batch.id,
        parsed=total_rows,
        new=credentials_new,
        duplicate=credentials_duplicate,
        sites_created=sites_created,
    )

    return BatchImportResult(
        batch_id=batch.id,
        parsed_rows=total_rows,
        sites_created=sites_created,
        sites_touched=sites_touched,
        credentials_new=credentials_new,
        credentials_duplicate=credentials_duplicate,
        skipped_invalid_rows=skipped_invalid,
    )


# ─── Per-batch validation ────────────────────────────────────────────


# Распределённый rate-limit (Redis) — общий между всеми worker-процессами.
# Раньше был in-process и не защищал от N параллельных процессов на один домен.
def _DomainRateLimiter(min_interval_s: float = DOMAIN_RATE_LIMIT_S):
    from infrastructure.rate_limit import RedisDomainRateLimiter

    return RedisDomainRateLimiter(min_interval_s)


async def _credentials_in_scope(
    session: AsyncSession, batch_id: int, scope: str
) -> list[WpCredential]:
    """scope: 'all' (все батча) | 'invalid' (только is_valid=false) | 'pending' (никогда не валидировались)."""
    stmt = (
        select(WpCredential)
        .where(
            WpCredential.import_batch_id == batch_id,
            WpCredential.deleted_at.is_(None),
        )
        .options(selectinload(WpCredential.site))
        .order_by(WpCredential.id)
    )
    if scope == "invalid":
        stmt = stmt.where(WpCredential.is_valid.is_(False))
    elif scope == "pending":
        stmt = stmt.where(WpCredential.last_validated_at.is_(None))
    rows = list((await session.execute(stmt)).scalars().all())
    # фильтр по живому/активному сайту
    return [c for c in rows if c.site is not None and c.site.is_active and c.site.deleted_at is None]


_SITE_FAILURE_KINDS: set[ErrorKind] = {
    ErrorKind.NETWORK,
    ErrorKind.SERVER_ERROR,
    ErrorKind.SITE_NOT_FOUND,
    ErrorKind.XMLRPC_DISABLED,
    ErrorKind.TASK_TIMEOUT,
    ErrorKind.PARKED,
    ErrorKind.CF_CHALLENGE,        # CDN/security блок — сайт нас не пускает
    ErrorKind.BROKEN_ENDPOINT,     # отдаёт мусор вместо XML — сайт неконсистентен
    # NB: RATE_LIMITED здесь НЕТ — это временно, не считаем site dead
}

# Kinds, на которые мы не тратим 10 ретраев — сразу выключаем сайт.
# Parking / suspended страница — это детерминированный ответ домена «здесь
# WP нет и не будет», ретраить остальные cred под этим site бессмысленно.
_SITE_INSTANT_DISABLE_KINDS: set[ErrorKind] = {
    ErrorKind.PARKED,
}


async def _apply_validation_result(
    session: AsyncSession, cred: WpCredential, outcome: ValidateOutcome
) -> tuple[bool, str]:
    """
    Применить результат к credential + обновить site-level failure tracking.

    Возвращает (changed_valid_flag, outcome_kind):
      kind ∈ {'ok', 'invalid', 'cooldown_skipped', 'transient', 'site_disabled'}

    Логика:
      - ok → cred valid, site счётчик сбрасываем (сайт жив)
      - auth_invalid / permission_denied → cred invalid (threshold=1, см.
        INVALIDATE_THRESHOLD). Site счётчик ТОЖЕ сбрасываем — сайт ответил
        XML-RPC и явно отказал, значит он жив.
      - network / server_error / site_not_found / xmlrpc_disabled → cred
        не меняем (transient), но бьём по site-счётчику. При достижении
        SITE_FAILURE_DISABLE_THRESHOLD выключаем сайт (is_active=False).
    """
    now = datetime.now(UTC)
    values: dict = {"last_validated_at": now}
    was_valid = cred.is_valid
    new_valid = was_valid

    raw_kind = outcome.error.value if outcome.error else "unknown"
    values["last_validation_kind"] = raw_kind
    values["last_error_message"] = outcome.error_message[:500] if outcome.error_message else None

    # Capability matrix: записываем что XML-RPC channel показал
    # - OK / AUTH_INVALID / PERMISSION_DENIED → endpoint живой, can_xmlrpc=True
    # - XMLRPC_DISABLED → can_xmlrpc=False
    # - NETWORK/TIMEOUT/PARKED — не знаем (None), не перезаписываем
    if outcome.error == ErrorKind.OK and outcome.valid_via == "admin_browser":
        # CF-сайт: cred подтверждён Patchright-логином. XML-RPC мы НЕ проверяли
        # (CF его резал) → can_xmlrpc не трогаем. Доказан admin-канал — он и
        # пойдёт в постинг (Tier 3). cf_protected на сайт ставим ниже.
        values["can_admin_login"] = True
        values["can_post_via_admin"] = True
        values["last_admin_check_at"] = now
    elif outcome.error == ErrorKind.OK:
        values["can_xmlrpc"] = True
        values["can_post_via_xmlrpc"] = True
    elif outcome.error in (ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED):
        values["can_xmlrpc"] = True
        values["can_post_via_xmlrpc"] = False
    elif outcome.error == ErrorKind.XMLRPC_DISABLED:
        values["can_xmlrpc"] = False
        values["can_post_via_xmlrpc"] = False

    site_disabled_now = False

    if outcome.success:
        values["is_valid"] = True
        values["error_counter"] = 0
        values["last_error_at"] = None
        values["error_cooldown_until"] = None
        values["last_error_message"] = None
        new_valid = True
        kind = "ok"
        # WP-роль из wp.getProfile (Tier 1). Пишем только если определили —
        # чтобы не затереть более раннюю/авторитетную роль значением None.
        if outcome.role:
            values["admin_role"] = outcome.role
            values["can_create_users"] = outcome.role == "administrator"
    elif outcome.error in DEFINITIVE_CRED_INVALID_KINDS:
        # Definitive ошибка — пароль не «починится» через 2 часа. Сразу bump
        # counter и при достижении threshold помечаем invalid. БЕЗ cooldown:
        # cooldown полезен только для recoverable ошибок (см. ниже).
        new_err = (cred.error_counter or 0) + 1
        values["error_counter"] = new_err
        values["last_error_at"] = now
        if new_err >= INVALIDATE_THRESHOLD:
            values["is_valid"] = False
            new_valid = False
            kind = "invalid"
        else:
            kind = "transient"
    elif outcome.error in _RECOVERABLE_KINDS:
        # Recoverable: сайт лежит/CF держит/прокси сдох — может ожить позже.
        # Cooldown 2ч чтобы параллельные корутины не били лишний раз и
        # background revalidator пропустил до окончания.
        if cred.error_cooldown_until and now < cred.error_cooldown_until:
            values["last_validation_kind"] = "cooldown_skipped"
            kind = "cooldown_skipped"
        else:
            values["last_error_at"] = now
            values["error_cooldown_until"] = now + COOLDOWN_AFTER_ERROR
            kind = "transient"
    else:
        # XMLRPC_DISABLED / SITE_NOT_FOUND / PARKED / BROKEN_ENDPOINT и т.п.
        # — конфигурационные/мёртвые проблемы, повтор через 2ч не поможет.
        # Cred-флаги не трогаем (это решает Tier 2 ветка); просто transient.
        kind = "transient"

    await session.execute(
        update(WpCredential).where(WpCredential.id == cred.id).values(**values)
    )

    # Append-only event log: пишем любое failure (не OK, не cooldown-skip).
    # Успехи не логируем — bounded объём (см. миграцию 0029).
    if not outcome.success and outcome.error and kind != "cooldown_skipped":
        from domain.site_events import record_site_event
        await record_site_event(
            session,
            site_id=cred.site_id,
            credential_id=cred.id,
            source="validation",
            error_kind=outcome.error.value,
            error_message=outcome.error_message,
        )

    # ─── Site-level updates ──────────────────────────────────────────
    if cred.site is not None:
        site_values: dict = {}
        # working URL — кеш для последующих посланий
        if outcome.working_xmlrpc_url:
            site_values["last_working_url"] = outcome.working_xmlrpc_url
            site_values["last_working_at"] = now
        # CF-сайт, прошли только браузером → метим, чтобы постинг сразу шёл в
        # Tier 3 (cached-session replay), минуя бесполезные request-first попытки.
        if outcome.valid_via == "admin_browser":
            site_values["cf_protected"] = True

        # Reset / increment site failure counter
        if outcome.success or outcome.error in (
            ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED
        ):
            # Сайт ответил (либо ok, либо явный auth-fail) → сайт жив
            if (cred.site.consecutive_site_failures or 0) > 0:
                site_values["consecutive_site_failures"] = 0
        elif outcome.error in _SITE_FAILURE_KINDS:
            # PARKED — один ответ достаточно: дальше по этому домену перебирать
            # остальные cred бессмысленно, домен мёртв или не наш.
            if outcome.error in _SITE_INSTANT_DISABLE_KINDS:
                new_count = SITE_FAILURE_DISABLE_THRESHOLD
            else:
                new_count = (cred.site.consecutive_site_failures or 0) + 1
            site_values["consecutive_site_failures"] = new_count
            site_values["last_site_failure_at"] = now
            site_values["last_site_failure_kind"] = outcome.error.value
            if (
                new_count >= SITE_FAILURE_DISABLE_THRESHOLD
                and cred.site.is_active
            ):
                # Per-cred guard: instant-disable kinds (parked) выключают всегда.
                # Для накопленных фейлов — не выключаем если у сайта есть другой
                # подтверждённо рабочий cred (кроме текущего, который сейчас fail).
                instant = outcome.error in _SITE_INSTANT_DISABLE_KINDS
                has_other_valid = await session.scalar(
                    select(WpCredential.id).where(
                        WpCredential.site_id == cred.site.id,
                        WpCredential.id != cred.id,
                        WpCredential.deleted_at.is_(None),
                        WpCredential.cred_status == "valid",
                    ).limit(1)
                )
                if instant or has_other_valid is None:
                    site_values["is_active"] = False
                    site_values["auto_disabled_at"] = now
                    site_disabled_now = True
                    kind = "site_disabled"

        if site_values:
            await session.execute(
                update(WpSite).where(WpSite.id == cred.site.id).values(**site_values)
            )

    await session.commit()

    if site_disabled_now:
        log.warning(
            "wp_batches.site_auto_disabled",
            site_id=cred.site.id if cred.site else None,
            domain=cred.site.domain if cred.site else None,
            failures=SITE_FAILURE_DISABLE_THRESHOLD,
            last_kind=outcome.error.value if outcome.error else None,
        )

    return (was_valid != new_valid, kind)


async def _detect_and_persist_language(
    session: AsyncSession, site: WpSite, client: httpx.AsyncClient
) -> None:
    """Один GET на homepage сайта + language detection. Если уже есть — не перетираем.

    `language_detected_at` пишется ВСЕГДА после попытки (даже при None) — чтобы
    в БД можно было отличить «не пробовали» (NULL) от «пробовали, не нашли»
    (timestamp + language=NULL).
    """
    if site.language:
        return
    # url для homepage: scheme + host из last_working_url или https default
    if site.last_working_url:
        from urllib.parse import urlparse

        parsed = urlparse(site.last_working_url)
        homepage = f"{parsed.scheme}://{parsed.netloc}/"
    else:
        homepage = f"https://{site.domain}/"
    try:
        lang = await detect_language(homepage, client=client, timeout=VALIDATE_TIMEOUT_S)
    except Exception as e:
        log.debug("lang.detect.exception", site_id=site.id, error=str(e))
        lang = None
    values = {"language_detected_at": datetime.now(UTC)}
    if lang:
        values["language"] = lang
    await session.execute(
        update(WpSite).where(WpSite.id == site.id).values(**values)
    )
    await session.commit()


async def _build_http_client(proxy: Proxy | None) -> httpx.AsyncClient:
    headers = {"User-Agent": random_ua()}
    proxy_url: str | None = None
    if proxy and proxy.is_active:
        from domain.proxies.service import proxy_url as _proxy_url
        proxy_url = _proxy_url(proxy)
    # verify=False — у дешёвого shared WP-хостинга часто истекшие/самоподписанные
    # сертификаты, иначе они бы отлетали как `network`. Креды мы и так уже
    # «знаем», MITM-риска валидации нет.
    return httpx.AsyncClient(
        timeout=VALIDATE_TIMEOUT_S,
        follow_redirects=True,
        headers=headers,
        proxy=proxy_url,
        verify=False,
    )


async def _build_http_client_url(proxy_url: str | None) -> httpx.AsyncClient:
    """Как _build_http_client, но принимает готовый proxy-URL — для путей, что
    сами выбрали residential exit (provision / links / post-ops)."""
    return httpx.AsyncClient(
        timeout=VALIDATE_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": random_ua()},
        proxy=proxy_url,
        verify=False,
    )


async def run_batch_validation(
    batch_id: int,
    *,
    scope: str = "all",
    concurrency: int = VALIDATE_DEFAULT_CONCURRENCY,
    proxy_id: int | None = None,
    detect_lang: bool = True,
    actor_id: int | None = None,
    level: str = "light",
    provision_after: bool = False,
    provision_role: str = "author",
) -> dict:
    """
    Главный entry-point для TaskIQ task.

    scope: 'all' | 'invalid' | 'pending'
    level:
      - 'light' — только XML-RPC валидация (Tier 1, ~1 запрос на cred). Default.
      - 'medium' — XML-RPC + admin form-login (Tier 2). Ловит cred-ы где
        XML-RPC отключён но wp-admin работает. +2-3 запроса на cred.
      - 'full' — medium + capability probes (theme-editor / widgets / pages /
        wp_version / role). +5-6 запросов на cred.
    """
    assert scope in ("all", "invalid", "pending")
    assert level in ("light", "medium", "full")

    # Провижн при валидации: чтобы корректно отобрать сайты под создание нашего
    # юзера, нужно ПОДТВЕРДИТЬ admin-логин (can_admin_login) и роль для каждого
    # креда — даже когда XML-RPC уже ответил OK. Поэтому форсим Tier 2 и
    # поднимаем light→medium.
    force_tier2 = False
    if provision_after:
        force_tier2 = True
        if level == "light":
            level = "medium"

    # 0. Load batch + proxy
    async with WriteSession() as s:
        batch = await get_batch(s, batch_id)
        if batch is None:
            return {"ok": False, "error": "batch not found"}
        if batch.status == WpBatchStatus.VALIDATING.value:
            return {"ok": False, "error": "already running"}

        # Прокси-стратегия: явный proxy_id → sticky (все креды через него,
        # back-compat). Иначе → ПУЛ active+unlocked, ротация по кредам + ретрай
        # дохлых (как постинг идёт через round-robin пул, а не один IP сервера).
        proxy_pool: list[Proxy] = []
        if proxy_id:
            sticky = await s.scalar(select(Proxy).where(Proxy.id == proxy_id))
            if sticky and sticky.is_active:
                proxy_pool = [sticky]
        else:
            _now0 = datetime.now(UTC)
            proxy_pool = list((await s.execute(
                select(Proxy).where(
                    Proxy.is_active.is_(True),
                    (Proxy.locked_until.is_(None)) | (Proxy.locked_until <= _now0),
                ).order_by(func.random()).limit(_MAX_PROXY_POOL)
            )).scalars().all())
            # webshare residential ротирует exit-IP на КАЖДЫЙ запрос даже на одном
            # порту, поэтому рабочей выборки портов хватает для разнообразия IP, а
            # кэш httpx-клиентов не разрастается до тысяч (по одному на прокси).

        await s.execute(
            update(WpImportBatch).where(WpImportBatch.id == batch_id).values(
                status=WpBatchStatus.VALIDATING.value,
                validation_started_at=datetime.now(UTC),
                validation_finished_at=None,
                pause_requested=False,
                valid_count=0,
                invalid_count=0,
                transient_count=0,
            )
        )
        await s.commit()

        creds = await _credentials_in_scope(s, batch_id, scope)

        # Сброс перевалидируемых кредов в pending («как только поставили») —
        # чистим вердикты/ошибки/capability, чтобы UI (бар/счётчики/строки,
        # и global queue) показывал прогресс ЭТОГО прогона, а не застывшие
        # старые статусы. Отдельной сессией, чтобы не экспайрить loaded `creds`
        # (их логин/пароль/site нужны дальше в цикле).
        if creds:
            async with WriteSession() as _rs:
                await _reset_creds_validation(_rs, [c.id for c in creds])
                await _rs.commit()

        # CF Tier 3: кап одновременных браузеров (Patchright) — из AppSettings,
        # настраивается под мощность сервера. Читаем один раз на батч.
        from domain.app_settings.service import get_app_settings
        _app_settings = await get_app_settings(s)
        cf_conc = _app_settings.cf_browser_concurrency

    rate_limiter = _DomainRateLimiter()
    sem = asyncio.Semaphore(max(1, concurrency))
    valid = 0
    invalid = 0
    transient = 0
    processed = 0

    # Кэш httpx-клиентов по прокси (один клиент на прокси, переиспользуем между
    # кредами). На каждый cred берём СЛУЧАЙНЫЙ прокси из пула (ротация).
    from domain.proxies.service import (
        proxy_url as _proxy_url,
        report_proxy_failure_by_url,
    )
    _client_cache: dict[int | None, tuple[httpx.AsyncClient, str | None]] = {}

    async def _get_client(px: Proxy | None) -> tuple[httpx.AsyncClient, str | None]:
        key = px.id if px else None
        if key not in _client_cache:
            http_c = await _build_http_client(px)
            purl_c = _proxy_url(px) if (px and px.is_active) else None
            _client_cache[key] = (http_c, purl_c)
        return _client_cache[key]

    def _pick_proxy(exclude_ids: set[int]) -> Proxy | None:
        avail = [p for p in proxy_pool if p.id not in exclude_ids]
        return random.choice(avail) if avail else None

    async def _report_proxy_dead(purl: str | None) -> None:
        if not purl:
            return
        try:
            async with WriteSession() as s_p:
                await report_proxy_failure_by_url(s_p, purl)
                await s_p.commit()
        except Exception as e:
            log.debug("validate.proxy_report_failed", error=str(e))

    async def _cf_browser_login(
        cred: WpCredential, pw: str, purl: str | None,
    ) -> bool:
        """Tier 3: один раз проходим CF + логинимся браузером (Patchright) на
        том же прокси/IP. Успех = cred валиден через admin-канал, сессия закеширована
        в Redis (постинг переиспользует её curl_cffi-реплеем). True если зашли."""
        from infrastructure.cf_browser import (
            browser_login_session, is_browser_available,
        )
        if not is_browser_available() or not cred.site:
            return False
        base = (cred.site.last_working_url or f"https://{cred.site.domain}")
        # base для логина — корень сайта, не xmlrpc-урл
        from urllib.parse import urlparse as _urlparse
        _p = _urlparse(base)
        base = f"{_p.scheme}://{_p.netloc}" if _p.netloc else f"https://{cred.site.domain}"
        try:
            sess = await browser_login_session(
                base, cred.login, pw, proxy_url=purl, concurrency=cf_conc,
            )
            return sess is not None
        except Exception as e:  # noqa: BLE001
            log.warning("validate.cf_browser.error", cred_id=cred.id, error=str(e)[:200])
            return False

    try:

        # Ошибки, похожие на ДОХЛЫЙ/забаненный прокси (обрыв до прокси, таймаут) —
        # ретраим через ДРУГОЙ прокси, до _PROXY_RETRIES. Если все дохлые → сайт.
        _PROXY_DEAD_KINDS = {ErrorKind.NETWORK, ErrorKind.TASK_TIMEOUT}
        _PROXY_RETRIES = 3

        async def _validate_one_work(
            cred: WpCredential,
        ) -> tuple[ValidateOutcome, httpx.AsyncClient | None, str | None]:
            """Tier1 XML-RPC с ротацией прокси + ретраем дохлых. Возвращает
            (outcome, http, purl) — клиент/прокси на котором получили результат
            (его же переиспользуют Tier2/lang)."""
            if cred.site:
                await rate_limiter.acquire(cred.site.domain)
            pw = decrypt_password(cred.password)
            tried: set[int] = set()
            http: httpx.AsyncClient | None = None
            purl: str | None = None
            outcome = ValidateOutcome(error=ErrorKind.NETWORK, error_message="no proxy")
            for _ in range(_PROXY_RETRIES):
                px = _pick_proxy(tried)
                if px is not None:
                    tried.add(px.id)
                http, purl = await _get_client(px)
                poster = XmlRpcPoster(http, timeout_seconds=VALIDATE_TIMEOUT_S, proxy_url=purl)
                try:
                    outcome = await poster.validate(site=cred.site, login=cred.login, password=pw)
                    # Tier 3 (CF): реальный CF-челлендж режет XML-RPC, лёгкого
                    # HTTP-обхода нет (Bot-Fight не выдаёт cf_clearance). Один раз
                    # проходим браузером — он же логинится в admin. Успех → cred
                    # валиден через admin-канал (valid_via), сайт метим cf_protected.
                    # XMLRPC_DISABLED/BROKEN/SERVER_ERROR браузером тут НЕ трогаем —
                    # для них сработает Tier 2 admin-login (дешевле браузера).
                    if outcome.error == ErrorKind.CF_CHALLENGE:
                        if await _cf_browser_login(cred, pw, purl):
                            outcome = ValidateOutcome(
                                error=ErrorKind.OK, valid_via="admin_browser",
                                role=outcome.role,
                            )
                except Exception as e:
                    log.exception("validate.unexpected", cred_id=cred.id, error=str(e))
                    outcome = ValidateOutcome(error=ErrorKind.NETWORK, error_message=f"unexpected: {e}")
                # Прокси-класс ошибки + это был реальный прокси + есть ещё непробованные
                # → репортим дохлый прокси и берём другой. Иначе — это вердикт сайта.
                if (outcome.error in _PROXY_DEAD_KINDS and purl is not None
                        and any(p.id not in tried for p in proxy_pool)):
                    await _report_proxy_dead(purl)
                    continue
                break
            return (outcome, http, purl)

        async def _persist_outcome(cred_id: int, outcome: ValidateOutcome) -> str:
            """Записать результат валидации. Вернуть kind. (Lang detection —
            отдельным шагом в _one, чтобы медленный homepage GET не съедал
            persist-бюджет и не убивал запись результата.)"""
            async with WriteSession() as s2:
                fresh = await s2.scalar(
                    select(WpCredential).where(WpCredential.id == cred_id)
                    .options(selectinload(WpCredential.site))
                )
                if fresh is None:
                    return "skipped"
                _, kind = await _apply_validation_result(s2, fresh, outcome)
                return kind

        # Сайты на которых детект бесполезен (точно мёртвые / не наши).
        _LANG_DEAD_KINDS = {
            ErrorKind.PARKED,
            ErrorKind.SITE_NOT_FOUND,
            ErrorKind.NETWORK,
            ErrorKind.TASK_TIMEOUT,
        }

        async def _maybe_detect_lang(
            cred_id: int, outcome: ValidateOutcome, http: httpx.AsyncClient,
        ) -> None:
            """Lang detection отдельным шагом со своим таймаутом. Запускаем
            НЕЗАВИСИМО от XML-RPC outcome (сайт может жить даже при
            xmlrpc_disabled). Пропускаем только если язык уже есть или сайт мёртв."""
            if not detect_lang or outcome.error in _LANG_DEAD_KINDS:
                return
            async with WriteSession() as s3:
                fresh = await s3.scalar(
                    select(WpCredential).where(WpCredential.id == cred_id)
                    .options(selectinload(WpCredential.site))
                )
                if fresh is None or fresh.site is None or fresh.site.language:
                    return
                try:
                    await _detect_and_persist_language(s3, fresh.site, http)
                except Exception as e:
                    log.warning("validate.lang.failed", error=str(e))

        # Tier 2: admin form-login — запускаем если уровень medium/full
        # И только когда XML-RPC ничего полезного не сказал про cred:
        # xmlrpc_disabled — может быть admin login работает (типичный случай)
        # broken_endpoint — XML-RPC отдаёт HTML вместо XML, тоже стоит пробовать
        # network / task_timeout / etc — попытаемся через wp-login.php
        # NB: для ok/auth_invalid/permission_denied/parked/rate_limited/captcha —
        #     XML-RPC уже дал decisive ответ, Tier 2 не нужен (экономим запросы).
        _TIER2_TRIGGER_KINDS = {
            ErrorKind.XMLRPC_DISABLED,
            ErrorKind.BROKEN_ENDPOINT,
            ErrorKind.NETWORK,
            ErrorKind.TASK_TIMEOUT,
            ErrorKind.SITE_NOT_FOUND,
            ErrorKind.SERVER_ERROR,
            ErrorKind.CF_CHALLENGE,
            ErrorKind.UNKNOWN,
        }

        async def _maybe_run_tier2(
            cred: WpCredential, tier1_outcome: ValidateOutcome,
            http: httpx.AsyncClient, purl: str | None,
        ) -> None:
            """
            После Tier 1 опционально дёрнем wp-admin для capability discovery.
            Не меняет основной 'kind' для UI — только обогащает can_* поля в БД.
            Идёт через ТОТ ЖЕ прокси/клиент, что и Tier 1 (рабочий).
            """
            if level == "light":
                return
            if (tier1_outcome.error not in _TIER2_TRIGGER_KINDS
                    and level == "medium" and not force_tier2):
                # Cred уже classified XML-RPC-ом, admin probe ничего не даст.
                # На level=full / force_tier2 всё равно идём — подтвердить
                # can_admin_login + роль (нужно для provision-author).
                return
            from infrastructure.wp_admin_client import (
                WpAdminClient, AdminLoginKind, LoginOutcome,
            )

            client = WpAdminClient(http, timeout_seconds=VALIDATE_TIMEOUT_S, proxy_url=purl)
            via_browser = False
            try:
                if cred.site:
                    await rate_limiter.acquire(cred.site.domain)
                pw = decrypt_password(cred.password)
                login_res = await client.login(
                    site=cred.site, login=cred.login, password=pw,
                )
                # Tier 3 (Patchright): расширенный триггер — не только CF_CHALLENGE,
                # но и UNKNOWN/SERVER_ERROR (generic WAF: 403 Forbidden, Access
                # Denied, JS interstitial без явных CF-маркеров). Лёгкого
                # HTTP-обхода нет → проходим браузером один раз; он же логинится.
                # Успех → синтезируем OK-login (даунстрим запишет can_admin_login,
                # is_valid) и метим сайт cf_protected.
                _TIER3_BROWSER_TRIGGERS = {
                    AdminLoginKind.CF_CHALLENGE,
                    AdminLoginKind.UNKNOWN,      # HTML 403/Access Denied/JS-redirect
                    AdminLoginKind.SERVER_ERROR, # 503 часто = CF rate limit
                }
                if login_res.error in _TIER3_BROWSER_TRIGGERS:
                    if await _cf_browser_login(cred, pw, purl):
                        via_browser = True
                        login_res = LoginOutcome(
                            error=AdminLoginKind.OK,
                            error_message="passed via browser (CF)",
                        )
            except Exception as e:
                log.warning("validate.tier2.error", cred_id=cred.id, error=str(e))
                return

            # Запись capability полей
            now = datetime.now(UTC)
            cred_values: dict = {"last_admin_check_at": now}
            site_values: dict = {}

            # Для UI/диагностики: краткая причина результата Tier 2.
            # Пишем в `last_error_message` ТОЛЬКО если Tier 1 был не-ok (т.е.
            # этот текст не затрёт реальную auth_invalid ошибку из XML-RPC).
            if tier1_outcome.error != ErrorKind.OK:
                tier2_summary = f"Tier 2 admin login: {login_res.error.value}"
                if login_res.error_message:
                    tier2_summary += f" — {login_res.error_message[:100]}"
                cred_values["last_error_message"] = tier2_summary[:500]

            # Финальное is_valid с учётом обоих tiers. Иначе при Tier 1 !=
            # OK/AUTH (xmlrpc_disabled / broken_endpoint / network …) и Tier 2
            # inconclusive cred остаётся с дефолтным is_valid=True (модель:
            # bool default True), что вводит UI в заблуждение.
            tier1_decisive = tier1_outcome.error in (
                ErrorKind.OK, ErrorKind.AUTH_INVALID, ErrorKind.PERMISSION_DENIED,
            )

            if login_res.error == AdminLoginKind.OK:
                cred_values["can_admin_login"] = True
                # Tier 2 OK → cred подтверждён. Сбрасываем error state.
                cred_values["is_valid"] = True
                cred_values["error_counter"] = 0
                cred_values["last_error_at"] = None
                cred_values["error_cooldown_until"] = None
                if tier1_outcome.error != ErrorKind.OK:
                    cred_values["last_error_message"] = None
                if via_browser:
                    # Прошли браузером (CF). REST/probe через httpx-клиент опять
                    # упрутся в CF → пропускаем, ставим admin-канал напрямую.
                    # Сессия закеширована — постинг пойдёт Tier 3 replay-ом.
                    cred_values["can_post_via_admin"] = True
                    site_values["cf_protected"] = True
                else:
                    # Роль + key-caps через REST users/me — дёшево (1 запрос),
                    # делаем уже на medium. Авторитетный источник роли и create_users.
                    try:
                        role, caps_map = await client.fetch_role_and_caps(cred.site)
                        if role:
                            cred_values["admin_role"] = role
                        if caps_map:
                            cred_values["can_create_users"] = bool(caps_map.get("create_users"))
                            # заодно обновим edit-caps если REST их отдал
                            if "edit_theme_options" in caps_map:
                                cred_values["can_edit_widgets"] = bool(caps_map["edit_theme_options"])
                            if "edit_themes" in caps_map:
                                cred_values["can_edit_themes"] = bool(caps_map["edit_themes"])
                    except Exception as e:
                        log.debug("validate.role_probe.failed", cred_id=cred.id, error=str(e))
                    # При успешном login запускаем capability probes только на level=full
                    if level == "full":
                        try:
                            caps = await client.probe_capabilities(cred.site)
                            if caps.can_post_via_admin is not None:
                                cred_values["can_post_via_admin"] = caps.can_post_via_admin
                            if caps.can_edit_pages is not None:
                                cred_values["can_edit_pages"] = caps.can_edit_pages
                            if caps.can_edit_themes is not None:
                                cred_values["can_edit_themes"] = caps.can_edit_themes
                            if caps.can_edit_widgets is not None:
                                cred_values["can_edit_widgets"] = caps.can_edit_widgets
                            if caps.admin_role:
                                cred_values["admin_role"] = caps.admin_role
                            # Site-level
                            if caps.wp_version:
                                site_values["wp_version"] = caps.wp_version
                            if caps.active_theme:
                                site_values["active_theme"] = caps.active_theme
                            if caps.file_editing_disabled is not None:
                                site_values["file_editing_disabled"] = caps.file_editing_disabled
                        except Exception as e:
                            log.warning("validate.tier2.probe_failed", cred_id=cred.id, error=str(e))
            elif login_res.error in (AdminLoginKind.AUTH_INVALID, AdminLoginKind.PERMISSION_DENIED):
                cred_values["can_admin_login"] = False
                # Tier 2 сказал bad creds → invalidate. НО только если Tier 1
                # (XML-RPC) сам не подтвердил валидность: при force_tier2 / level=full
                # admin-логин может упасть из-за защиты wp-login (captcha/2FA/IP),
                # а креды при этом рабочие (XML-RPC их авторизовал). Тогда хватает
                # can_admin_login=False — сам cred остаётся valid.
                if tier1_outcome.error != ErrorKind.OK:
                    cred_values["is_valid"] = False
            elif login_res.error == AdminLoginKind.CF_CHALLENGE:
                site_values["cf_protected"] = True
                # cred_values не трогаем — мы не дошли до login
                # Если оба tier inconclusive И нет prior trust — not_confirmed
                if not tier1_decisive and cred.can_admin_login is not True:
                    cred_values["is_valid"] = False
            elif login_res.error == AdminLoginKind.PARKED:
                # XML-RPC уже мог это записать, но если нет — site точно мёртв
                cred_values["can_admin_login"] = False
                cred_values["is_valid"] = False
            else:
                # NETWORK / LOGIN_DISABLED / UNKNOWN / SERVER_ERROR / SITE_NOT_FOUND
                # — Tier 2 inconclusive. Помечаем not_confirmed ТОЛЬКО если:
                #   1) Tier 1 тоже не decisive (нет OK/AUTH)
                #   2) И нет prior подтверждения can_admin_login=True
                # Иначе сохраняем prior trust — текущий неудачный attempt
                # мог быть из-за случайной 415/timeout/etc.
                if not tier1_decisive and cred.can_admin_login is not True:
                    cred_values["is_valid"] = False

            # Сайт ОТВЕТИЛ через админку (ok / явный auth-fail) → он жив, даже
            # если Tier 1 был xmlrpc_disabled / CF / network. Сбрасываем
            # преждевременно накрученный на Tier 1 счётчик провалов сайта — иначе
            # сайт, рабочий через admin (но с выключенным xmlrpc), мог бы
            # отключиться по «xmlrpc disabled». Site-disable теперь честный:
            # только когда ВСЯ цепочка (xmlrpc → admin → браузер) недоступна.
            if login_res.error in (AdminLoginKind.OK, AdminLoginKind.AUTH_INVALID,
                                   AdminLoginKind.PERMISSION_DENIED):
                site_values["consecutive_site_failures"] = 0
                site_values["last_site_failure_at"] = None
                site_values["last_site_failure_kind"] = None

            # Запись
            async with WriteSession() as s_t2:
                await s_t2.execute(
                    update(WpCredential).where(WpCredential.id == cred.id).values(**cred_values)
                )
                if site_values and cred.site:
                    await s_t2.execute(
                        update(WpSite).where(WpSite.id == cred.site.id).values(**site_values)
                    )
                await s_t2.commit()

            # Инкрементальный provision: как только подтвердили валидный admin с
            # create_users — СРАЗУ создаём наш аккаунт (по ходу проверки, не ждём
            # конца батча). provision_site идемпотентен (skip_exists).
            if (provision_after and cred_values.get("can_admin_login")
                    and cred_values.get("can_create_users") and cred.site):
                try:
                    from domain.wp_provision.service import provision_site
                    pr = await provision_site(cred.site.id, role=provision_role, actor_id=actor_id)
                    if pr.get("status") == "created":
                        log.info("validate.provision.inline", cred_id=cred.id,
                                 site_id=cred.site.id, domain=pr.get("domain"))
                except Exception as e:
                    log.warning("validate.provision.inline.failed", cred_id=cred.id, error=str(e))

            log.info(
                "validate.tier2.done",
                cred_id=cred.id,
                login_kind=login_res.error.value,
                wrote_cred=list(cred_values.keys()),
                wrote_site=list(site_values.keys()),
            )

        async def _one(cred: WpCredential) -> None:
            nonlocal valid, invalid, transient, processed
            async with sem:
                # pause check
                async with WriteSession() as s_pp:
                    fresh_batch = await get_batch(s_pp, batch_id)
                    if fresh_batch is None or fresh_batch.pause_requested:
                        return

                # Жёсткий per-cred timeout. Если httpx залип/прокся повесила
                # соединение — wait_for бросит CancelledError, _validate_one_work
                # отменится, `async with sem:` корректно выходит → слот свободен.
                # Без этого один зависший cred держит слот навсегда.
                _http: httpx.AsyncClient | None = None
                _purl: str | None = None
                try:
                    outcome, _http, _purl = await asyncio.wait_for(
                        _validate_one_work(cred), timeout=VALIDATE_PER_CRED_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "validate.cred_timeout",
                        cred_id=cred.id,
                        domain=cred.site.domain if cred.site else None,
                        timeout_s=VALIDATE_PER_CRED_TIMEOUT_S,
                    )
                    outcome = ValidateOutcome(
                        error=ErrorKind.TASK_TIMEOUT,
                        error_message=f"hard timeout {VALIDATE_PER_CRED_TIMEOUT_S}s",
                    )

                # Запись результата отдельной wait_for-обёрткой — не должна тянуть
                # больше нескольких секунд (один SQL), но защищаемся от хвоста.
                try:
                    kind = await asyncio.wait_for(
                        _persist_outcome(cred.id, outcome), timeout=30
                    )
                except asyncio.TimeoutError:
                    log.warning("validate.persist_timeout", cred_id=cred.id)
                    kind = "transient"
                except Exception as e:
                    log.exception("validate.persist_failed", cred_id=cred.id, error=str(e))
                    kind = "transient"

                if kind == "ok":
                    valid += 1
                elif kind == "invalid":
                    invalid += 1
                elif kind != "skipped":
                    transient += 1
                processed += 1

                # Tier2/lang идут через клиент/прокси Tier1. Если Tier1 отвалился
                # по таймауту (_http=None) — берём свежий из пула.
                if _http is None:
                    _http, _purl = await _get_client(_pick_proxy(set()))

                # Tier 2 (admin form-login + capability probes) — опционально,
                # отдельной wait_for-обёрткой чтобы не задерживать счётчики.
                if level in ("medium", "full"):
                    try:
                        await asyncio.wait_for(
                            _maybe_run_tier2(cred, outcome, _http, _purl),
                            timeout=VALIDATE_PER_CRED_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        log.warning("validate.tier2.timeout", cred_id=cred.id)
                    except Exception as e:
                        log.exception("validate.tier2.failed", cred_id=cred.id, error=str(e))

                # Lang detection — отдельным шагом со своим таймаутом, чтобы
                # медленный homepage GET не убивал запись результата валидации.
                try:
                    await asyncio.wait_for(
                        _maybe_detect_lang(cred.id, outcome, _http),
                        timeout=VALIDATE_TIMEOUT_S + 5,
                    )
                except asyncio.TimeoutError:
                    log.warning("validate.lang.timeout", cred_id=cred.id)
                except Exception as e:
                    log.warning("validate.lang.step_failed", cred_id=cred.id, error=str(e))

                # Периодически обновляем counters на батче (каждые 10 cred)
                if processed % 10 == 0:
                    async with WriteSession() as s3:
                        await s3.execute(
                            update(WpImportBatch).where(WpImportBatch.id == batch_id).values(
                                valid_count=valid,
                                invalid_count=invalid,
                                transient_count=transient,
                            )
                        )
                        await s3.commit()

        await asyncio.gather(*(_one(c) for c in creds), return_exceptions=False)
    finally:
        # Закрываем все httpx-клиенты из кэша (по одному на прокси).
        for _http_c, _ in _client_cache.values():
            try:
                await _http_c.aclose()
            except Exception:
                pass

    # Финализация
    async with WriteSession() as s_fin:
        fresh_batch = await get_batch(s_fin, batch_id)
        was_paused = fresh_batch.pause_requested if fresh_batch else False
        if not was_paused:
            # Креды, оставшиеся pending на ОТКЛЮЧЁННЫХ сайтах: сайт исключён из
            # scope валидации (is_active=false), проверить их нечем и незачем —
            # они не «ждут проверки». Метим терминально site_disabled → invalid,
            # чтобы не висели фантомным pending при DONE (live-счётчики посчитают
            # их в invalid).
            await s_fin.execute(
                update(WpCredential)
                .where(
                    WpCredential.import_batch_id == batch_id,
                    WpCredential.deleted_at.is_(None),
                    WpCredential.last_validated_at.is_(None),
                    WpCredential.site_id.in_(
                        select(WpSite.id).where(WpSite.is_active.is_(False))
                    ),
                )
                .values(
                    is_valid=False,
                    last_validation_kind="site_disabled",
                    last_validated_at=datetime.now(UTC),
                    last_error_message="сайт отключён (недоступен / parked)",
                )
            )
        await s_fin.execute(
            update(WpImportBatch).where(WpImportBatch.id == batch_id).values(
                status=(
                    WpBatchStatus.PAUSED.value if was_paused
                    else WpBatchStatus.DONE.value
                ),
                validation_finished_at=datetime.now(UTC),
                valid_count=valid,
                invalid_count=invalid,
                transient_count=transient,
                pause_requested=False,
            )
        )
        await s_fin.commit()

    log.info(
        "wp_batches.validate.done",
        batch_id=batch_id,
        scope=scope,
        valid=valid,
        invalid=invalid,
        transient=transient,
        was_paused=was_paused,
        actor_id=actor_id,
    )
    result = {
        "ok": True,
        "batch_id": batch_id,
        "valid": valid,
        "invalid": invalid,
        "transient": transient,
        "processed": processed,
        "total": len(creds),
        "paused": was_paused,
    }

    # Опционально: сразу после валидации создаём наши аккаунты на свежих
    # admin-сайтах (чекбокс «provision» в окне валидации). Не падаем на ошибке.
    if provision_after and not was_paused:
        try:
            from domain.wp_provision import run_batch_provision
            prov = await run_batch_provision(
                batch_id, role=provision_role, actor_id=actor_id,
            )
            result["provision"] = {k: prov.get(k) for k in
                                   ("total", "created", "skipped", "failed")}
        except Exception as e:
            log.warning("wp_batches.provision_after.failed", batch_id=batch_id, error=str(e))
    return result


async def request_pause(session: AsyncSession, batch_id: int) -> None:
    await session.execute(
        update(WpImportBatch).where(WpImportBatch.id == batch_id).values(pause_requested=True)
    )
    await session.commit()


async def _reset_creds_validation(session: AsyncSession, cred_ids: list[int]) -> None:
    """Сброс вердиктов/ошибок/capability у конкретных кредов → cred_status='pending'
    («как только поставили»). Зовём при старте (re-)validate, чтобы бар/счётчики/
    строки честно показывали прогресс прогона, а не застывшие старые вердикты.
    Чанкуем по 1000 (лимит bind-параметров PG)."""
    for i in range(0, len(cred_ids), 1000):
        chunk = cred_ids[i:i + 1000]
        await session.execute(
            update(WpCredential)
            .where(WpCredential.id.in_(chunk))
            .values(
                is_valid=True,            # + last_validated_at=None → cred_status='pending'
                last_validated_at=None,
                last_validation_kind=None,
                last_error_message=None,
                last_error_at=None,
                error_counter=0,
                error_cooldown_until=None,
                can_xmlrpc=None,
                can_admin_login=None,
                can_post_via_xmlrpc=None,
                can_post_via_admin=None,
                can_create_users=None,
                admin_role=None,
                last_admin_check_at=None,
            )
        )


async def reset_batch_validation(session: AsyncSession, batch_id: int) -> int:
    """Полный сброс валидации батча: все creds → pending (как сразу после
    импорта), счётчики/таймстампы батча обнулены, статус → uploaded.

    Деструктивно: стирает все вердикты (valid/invalid/transient), capability-
    матрицу (Tier 1/2 discovery) и cooldown'ы. НЕ трогает provision-флаги,
    amount_use и сами логин/пароль. Нужен для чистого повторного прогона
    (напр. когда первая проверка прошла неполным уровнем). Возвращает число
    затронутых creds. После сброса нужен новый запуск validate (full)."""
    res = await session.execute(
        update(WpCredential)
        .where(
            WpCredential.import_batch_id == batch_id,
            WpCredential.deleted_at.is_(None),
        )
        .values(
            is_valid=True,            # + last_validated_at=None → cred_status='pending'
            last_validated_at=None,
            last_validation_kind=None,
            last_error_message=None,
            last_error_at=None,
            error_counter=0,
            error_cooldown_until=None,
            # capability-матрица (Tier 1/2 discovery) — сброс
            can_xmlrpc=None,
            can_admin_login=None,
            can_post_via_xmlrpc=None,
            can_post_via_admin=None,
            can_create_users=None,
            admin_role=None,
            last_admin_check_at=None,
        )
    )
    await session.execute(
        update(WpImportBatch).where(WpImportBatch.id == batch_id).values(
            status=WpBatchStatus.UPLOADED.value,
            valid_count=0,
            invalid_count=0,
            transient_count=0,
            validation_started_at=None,
            validation_finished_at=None,
            pause_requested=False,
        )
    )
    await session.commit()
    return res.rowcount or 0


# ─── Result CSV ─────────────────────────────────────────────────────


async def iter_batch_result_rows(
    session: AsyncSession,
    batch_id: int,
    *,
    status_filter: str | None = None,
):
    """Стримим credentials батча с их результатами.

    status_filter (опционально, для export): 'valid' | 'invalid' | 'transient' |
    'pending' — те же условия что в endpoint `/credentials`. Для 'duplicates'
    стримим оригиналы из `batch.duplicate_cred_ids`.
    """
    if status_filter == "duplicates":
        # Берём cred-«оригиналы» из сохранённого списка ID
        batch = await session.scalar(
            select(WpImportBatch).where(WpImportBatch.id == batch_id)
        )
        dup_ids = list((batch.duplicate_cred_ids if batch else None) or [])
        if not dup_ids:
            return
        rows = (await session.execute(
            select(WpCredential)
            .where(WpCredential.id.in_(dup_ids), WpCredential.deleted_at.is_(None))
            .options(selectinload(WpCredential.site))
            .order_by(WpCredential.id)
        )).scalars().all()
        for cred in rows:
            yield cred
        return

    after = 0
    while True:
        stmt = (
            select(WpCredential)
            .where(
                WpCredential.import_batch_id == batch_id,
                WpCredential.deleted_at.is_(None),
                WpCredential.id > after,
            )
            .options(selectinload(WpCredential.site))
            .order_by(WpCredential.id)
            .limit(500)
        )
        if status_filter in ("valid", "invalid", "transient", "pending"):
            stmt = stmt.where(WpCredential.cred_status == status_filter)
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return
        for cred in rows:
            yield cred
        after = rows[-1].id
        if len(rows) < 500:
            return


# ─── Aggregate counters ──────────────────────────────────────────────


async def stats(session: AsyncSession) -> dict:
    rows = (await session.execute(
        select(WpImportBatch.status, func.count(WpImportBatch.id))
        .where(WpImportBatch.deleted_at.is_(None))
        .group_by(WpImportBatch.status)
    )).all()
    return {str(r[0]): int(r[1]) for r in rows}
