"""
Celery task для самого постинга прогона.

Архитектура (см. ADR-003, ADR-009):
- Одна Celery task = один прогон. Воркер берёт run_id, поднимает asyncio loop
  внутри, опрашивает PG порциями pending text_items и постит параллельно
  через семафор размера `run.concurrency`.
- Site claim registry (in-memory per task): чтобы 25 параллельных корутин не
  выбрали один и тот же site из пула. Лок на site_id берётся до УСПЕХА или
  до решения «все credentials провалились» — затем сайт «использован» (через
  project_wp_used) или «исчерпан» (только на этот run).
- Self-healing discovery: при 404/auth_invalid сбрасываем кеш URL сайта,
  следующий пост запустит discovery заново. AUTH_INVALID помечает credential
  как невалидную (не сайт).
- Heartbeat: каждые HEARTBEAT_INTERVAL_S отдельная корутина обновляет
  `posting_runs.worker_heartbeat_at`. Recovery job ловит зависшие runs.
- Pause/Cancel: между батчами проверяем `pause_requested`/`cancel_requested`,
  обновлённые через API. При pause — sleep + повторная проверка; cancel — выход.
- Денорм-счётчики обновляются атомарно: `posted_count = posted_count + 1`.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import and_, exists, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.celery_app import celery_app
from core.crypto import decrypt_password
from core.db import WriteSession, write_engine
from core.metrics import (
    gym_posting_xmlrpc_duration_seconds,
    gym_posting_xmlrpc_requests_total,
)
from core.realtime import publish_run_event
from core.storage import storage
from infrastructure.db.models import (
    PostingRun,
    PostingRunStatus,
    ProjectWpUsed,
    Proxy,
    RunTaskType,
    TextItem,
    TextItemStatus,
    WpCredential,
    WpSite,
)
from infrastructure.concurrency import posting_limiter
from infrastructure.wp_client import (
    DEFINITIVE_CRED_INVALID_KINDS,
    ErrorKind,
    PostOutcome,
    XmlRpcPoster,
)

log = structlog.get_logger(__name__)


async def _read_global_posting_limit() -> int:
    """Глобальный потолок одновременных постов (across runs/процессы) из
    app_settings. Fallback 80, если строки нет/ошибка."""
    try:
        async with WriteSession() as s:
            from domain.app_settings.service import get_app_settings
            row = await get_app_settings(s)
            return int(row.global_posting_concurrency)
    except Exception:
        return 80


async def _read_posting_tuning() -> tuple[int, int, int, int]:
    """Тюнинг постинга из app_settings:
    (global_limit, floor, site_disable_threshold, site_disable_threshold_cf).
    Fallback на дефолты, если строки нет/ошибка."""
    try:
        async with WriteSession() as s:
            from domain.app_settings.service import get_app_settings
            cfg = await get_app_settings(s)
            return (
                int(cfg.global_posting_concurrency),
                int(cfg.posting_concurrency_floor),
                int(cfg.site_disable_threshold),
                int(cfg.site_disable_threshold_cf),
            )
    except Exception:
        return (80, DEFAULT_POSTING_FLOOR, DEFAULT_SITE_DISABLE_THRESHOLD,
                DEFAULT_SITE_DISABLE_THRESHOLD_CF)


async def _count_active_posting_runs() -> int:
    """Сколько прогонов сейчас реально крутятся (status=running) — для дележа
    global-ёмкости между ними (fair-share). Fallback 1 при ошибке."""
    try:
        async with WriteSession() as s:
            n = await s.scalar(
                select(func.count(PostingRun.id)).where(
                    PostingRun.status == PostingRunStatus.RUNNING.value
                )
            )
        return max(1, int(n or 1))
    except Exception:
        return 1


async def _effective_concurrency(*, global_limit: int, ceiling: int, floor: int) -> int:
    """Адаптивная конкурентность одного прогона (work-conserving fair-share):
    clamp(global // активные_прогоны, floor, ceiling). Один прогон в одиночку
    забивает сервер; при многих делится честно, никто не голодает."""
    active = await _count_active_posting_runs()
    share = global_limit // max(1, active)
    return max(1, min(ceiling, max(floor, share)))


# ─── Конфигурация воркера ─────────────────────────────────────────────

BATCH_SIZE = 50                 # сколько text_items берём за раз из PG
HEARTBEAT_INTERVAL_S = 10       # как часто пишем worker_heartbeat_at
CONTROL_POLL_INTERVAL_S = 5     # как часто проверяем pause/cancel
PAUSE_SLEEP_S = 5               # пауза между пере-проверками pause_requested
MAX_CREDS_PER_SITE = 5          # сколько credential перебираем на одном site
                                # перед тем как пометить site «exhausted for run»
CREDENTIAL_ERROR_THRESHOLD = 1  # AUTH_INVALID детерминированно → сразу is_valid=False
# Fallback-дефолты тюнинга (если app_settings недоступны). Тюнятся в /settings.
DEFAULT_POSTING_FLOOR = 5
DEFAULT_SITE_DISABLE_THRESHOLD = 25
DEFAULT_SITE_DISABLE_THRESHOLD_CF = 8
TARGET_RECALC_S = 5.0           # как часто пересчитываем адаптивный target окна
NO_PROGRESS_CHECK_S = 15.0      # окно без прогресса → проверка «кончились сайты»
# Пароль не «починится» сам через retry — нет смысла копить fails, помечаем сразу.
# Это согласовано с INVALIDATE_THRESHOLD=1 в validate (см. wp_batches/service.py).


# ─── Site claim registry ──────────────────────────────────────────────


class SiteClaimRegistry:
    """
    Per-run in-memory реестр «занятых» сайтов.

    Лок на site_id даёт мьютекс — пока одна корутина перебирает credentials
    этого сайта, другие к нему не лезут. Если все credentials провалились —
    site помечается «exhausted for this run», следующие корутины его пропускают.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._exhausted: set[int] = set()

    def is_exhausted(self, site_id: int) -> bool:
        return site_id in self._exhausted

    def mark_exhausted(self, site_id: int) -> None:
        self._exhausted.add(site_id)

    @asynccontextmanager
    async def claim(self, site_id: int):
        lock = self._locks[site_id]
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()


# ─── Запросы к БД ─────────────────────────────────────────────────────


async def _pick_pending_batch(
    session: AsyncSession, run_id: int, limit: int, *, require_text: bool = False
) -> list[TextItem]:
    """
    Атомарно забрать пачку pending text_items и пометить их `posting`.

    Используем SKIP LOCKED — на случай если несколько процессов случайно
    смотрят на один run (защита от race; ожидаемое поведение — один воркер).

    require_text=True (post-тип): берём только айтемы с готовым контентом
    (text_id ИЛИ storage_key) — чтобы пайплайн/manual постил только то, что уже
    сгенерировано, а пустые gen-айтемы пропускал. Для link-типа False (там
    контент — это link_url, текста нет).
    """
    now = datetime.now(UTC)
    conds = [
        TextItem.posting_run_id == run_id,
        TextItem.status == TextItemStatus.PENDING.value,
        # drip-feed: берём только «созревшие» задачи
        or_(TextItem.not_before.is_(None), TextItem.not_before <= now),
    ]
    if require_text:
        conds.append(or_(TextItem.text_id.isnot(None), TextItem.storage_key.isnot(None)))
    pick = (
        select(TextItem.id)
        .where(*conds)
        .order_by(TextItem.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(pick)).scalars().all())
    if not ids:
        return []

    await session.execute(
        update(TextItem)
        .where(TextItem.id.in_(ids))
        .values(status=TextItemStatus.POSTING.value)
    )
    await session.commit()

    rows = (
        await session.execute(
            select(TextItem).where(TextItem.id.in_(ids)).order_by(TextItem.id)
        )
    ).scalars().all()
    return list(rows)


async def _external_gen_pending(run_id: int) -> int:
    """Сколько ещё несгенерённых айтемов ждут ВНЕШНЮЮ генерацию, идущую прямо
    сейчас (флаг gen_params.gen_active, который ставит/снимает generate_run_items).
    Стрим-постинг (manual gen_per_post «Старт постинга» поверх «Сгенерировать
    тексты»): пока > 0 — постинг ждёт новые готовые тексты, не финишируя.
    Возвращает 0, если генерация не активна (флаг снят) — даже при наличии пустых
    айтемов (их никто не наполнит → не зависаем)."""
    async with WriteSession() as s:
        gp = await s.scalar(select(PostingRun.gen_params).where(PostingRun.id == run_id))
        if not (gp or {}).get("gen_active"):
            return 0
        ungen = await s.scalar(select(func.count(TextItem.id)).where(
            TextItem.posting_run_id == run_id, TextItem.text_id.is_(None),
            TextItem.status.in_(
                [TextItemStatus.PENDING.value, TextItemStatus.GENERATING.value])))
    return int(ungen or 0)


def _run_site_filter(run):
    """(site_langs, site_tlds, site_tags, site_domains) из gen_params рана —
    фильтр пула сайтов: язык / TLD / теги кредов / явный список доменов.
    Домены могут лежать inline или файлом в MinIO (resolve_site_domains)."""
    from domain.postings.service import resolve_site_domains
    gp = getattr(run, "gen_params", None) or {}
    return (
        gp.get("site_langs") or None,
        gp.get("site_tlds") or None,
        gp.get("site_tags") or None,
        resolve_site_domains(gp),
    )


async def _pick_candidate_sites(
    session: AsyncSession,
    *,
    project_id: int,
    run_id: int,
    exclude_site_ids: set[int],
    limit: int,
    site_langs: list[str] | None = None,
    site_tlds: list[str] | None = None,
    site_tags: list[str] | None = None,
    site_domains: list[str] | None = None,
) -> list[WpSite]:
    """
    Сайты которые:
    - активны и не удалены
    - имеют хотя бы одну валидную credential
    - в этом проекте использованы СТРОГО МЕНЬШЕ чем projects.max_posts_per_site
    - не в `exclude_site_ids` (in-memory: exhausted в текущем run + только
      что взятые конкурирующими корутинами; финальное решение всё равно
      под per-site lock-ом)

    Лимит `max_posts_per_site` задаётся per-ЗАДАЧА (posting_runs), дефолт 1 =
    классическое «1 site = 1 пост». Воркер читает значение live: если поднять
    лимит задачи во время/после run-а, ранее «исчерпанные» сайты снова станут
    eligible на следующей итерации подбора (можно добрать сайты).
    """
    # Только сайты с ПОДТВЕРЖДЁННО постабельным cred: cred_status='valid' (логин
    # ок) И подтверждённый КАНАЛ постинга (xmlrpc ИЛИ admin). Иначе в постинг
    # лезут сайты, где валидация уже решила, что постить нельзя (can_post_via_*
    # = False/NULL) — они стабильно падают и жгут попытки/таймауты.
    cred_conds = [
        WpCredential.site_id == WpSite.id,
        WpCredential.cred_status == "valid",
        WpCredential.deleted_at.is_(None),
        or_(
            WpCredential.can_post_via_xmlrpc.is_(True),
            WpCredential.can_post_via_admin.is_(True),
        ),
    ]
    # Пул по тегам: сайт подходит, если у него есть валидный постабельный cred
    # с одним из выбранных тегов (теги живут на credential, не на site).
    if site_tags:
        cred_conds.append(or_(*(WpCredential.tags.any(t) for t in site_tags)))
    has_valid_cred = exists().where(and_(*cred_conds))
    # Подзапрос: сколько раз этот сайт уже использован в этом проекте
    used_count_subq = (
        select(func.count(ProjectWpUsed.id))
        .where(
            ProjectWpUsed.project_id == project_id,
            ProjectWpUsed.site_id == WpSite.id,
        )
        .correlate(WpSite)
        .scalar_subquery()
    )
    # max_posts_per_site ЗАДАЧИ (один scalar для всех сайтов; live-read —
    # поднятие лимита задачи сразу расширяет пул кандидатов)
    max_reuse_subq = (
        select(PostingRun.max_posts_per_site).where(PostingRun.id == run_id).scalar_subquery()
    )

    stmt = (
        select(WpSite)
        .options(selectinload(WpSite.credentials))
        .where(
            WpSite.deleted_at.is_(None),
            WpSite.is_active.is_(True),
            has_valid_cred,
            used_count_subq < max_reuse_subq,
        )
        # LRU + random: сначала давно/никогда не использованные сайты (ровный
        # делёж бэклинков по пулу со временем), random() как tiebreak — чтобы
        # 25 параллельных айтемов не толпились на одних и тех же id и не долбили
        # одни сайты concurrently (отсюда были 503/500). Порядок на корректность
        # не влияет (каждый сайт всё равно используется max_posts_per_site раз).
        .order_by(WpSite.last_used_at.asc().nulls_first(), func.random())
        .limit(limit)
    )
    if exclude_site_ids:
        stmt = stmt.where(not_(WpSite.id.in_(exclude_site_ids)))
    # Фильтр пула: язык сайта ∈ site_langs и TLD домена ∈ site_tlds (если заданы).
    if site_langs:
        stmt = stmt.where(WpSite.language.in_(site_langs))
    if site_tlds:
        stmt = stmt.where(or_(*(WpSite.domain.ilike(f"%.{t}") for t in site_tlds)))
    # Свой список доменов: постим строго на эти домены (нормализованы при сохранении).
    if site_domains:
        stmt = stmt.where(WpSite.domain.in_(site_domains))

    return list((await session.execute(stmt)).scalars().unique().all())


async def _bump_run_counter(
    session: AsyncSession, run_id: int, field: str
) -> None:
    """
    Атомарный +1 на posted_count/failed_count/skipped_count.
    Шлёт SSE-событие progress в Redis канал run:{id} после commit-а.
    """
    assert field in ("posted_count", "failed_count", "skipped_count")
    await session.execute(
        update(PostingRun)
        .where(PostingRun.id == run_id)
        .values(
            **{field: getattr(PostingRun, field) + 1},
            last_progress_at=datetime.now(UTC),
        )
    )
    await session.commit()

    # Снимок текущих счётчиков для UI
    row = (
        await session.execute(
            select(
                PostingRun.posted_count,
                PostingRun.failed_count,
                PostingRun.skipped_count,
                PostingRun.total_texts,
                PostingRun.status,
            ).where(PostingRun.id == run_id)
        )
    ).one_or_none()
    if row:
        await publish_run_event(
            run_id,
            "progress",
            {
                "posted": int(row[0]),
                "failed": int(row[1]),
                "skipped": int(row[2]),
                "total": int(row[3]),
                "status": str(row[4]),
                "bumped": field,
            },
        )


async def _reconcile_run_counters(session: AsyncSession, run_id: int) -> None:
    """Пересчитать posted/failed/skipped из ФИНАЛЬНЫХ статусов text_items.

    Денормализованные счётчики бампятся на каждую попытку постинга, поэтому при
    ретраях posted+failed может превысить total (прогресс >100%). Перед
    финализацией приводим счётчики к фактическому числу итемов в каждом
    терминальном статусе — это truth (sum == total)."""
    rows = (
        await session.execute(
            select(TextItem.status, func.count())
            .where(TextItem.posting_run_id == run_id)
            .group_by(TextItem.status)
        )
    ).all()
    counts = {st: int(c) for st, c in rows}
    await session.execute(
        update(PostingRun).where(PostingRun.id == run_id).values(
            posted_count=counts.get(TextItemStatus.POSTED.value, 0),
            failed_count=counts.get(TextItemStatus.FAILED.value, 0),
            skipped_count=counts.get(TextItemStatus.SKIPPED.value, 0),
        )
    )


def _is_post_type(run) -> bool:
    """post-тип (не sitewide/homepage link) — только к таким применяем post_verify."""
    return run.task_type not in (
        RunTaskType.SITEWIDE_LINK.value, RunTaskType.HOMEPAGE_LINK.value)


async def _annotate_post_verify(
    session: AsyncSession, item_id: int, found: bool, resolved_url: str | None
) -> None:
    """mark-режим: проставить ✓/✗ + момент проверки + резолвленный permalink."""
    vals: dict = {
        "link_verified": found, "verified_at": datetime.now(UTC),
        "verify_attempts": TextItem.verify_attempts + 1,
    }
    if resolved_url:
        vals["posted_url"] = resolved_url
    await session.execute(update(TextItem).where(TextItem.id == item_id).values(**vals))
    await session.commit()


async def _mark_text_posted(
    session: AsyncSession,
    *,
    item: TextItem,
    site: WpSite,
    credential: WpCredential,
    outcome: PostOutcome,
    project_id: int,
    run_id: int,
) -> None:
    """Успешный пост: обновляем text_item, credential, site, проект, счётчики."""
    now = datetime.now(UTC)

    await session.execute(
        update(TextItem)
        .where(TextItem.id == item.id)
        .values(
            status=TextItemStatus.POSTED.value,
            posted_url=outcome.posted_url,
            post_id=outcome.post_id,
            credential_id=credential.id,
            site_id=site.id,
            posted_at=now,
            attempts=TextItem.attempts + 1,
            last_error=None,
        )
    )
    # Eager capability marking — ТОЛЬКО для канала который реально сработал.
    # Раньше всегда ставили can_xmlrpc=True даже когда пост ушёл через admin →
    # cred ошибочно помечался xmlrpc-рабочим, и следующий auto-пост опять
    # начинал с xmlrpc → лишний фейл → опять fallback.
    # Self-heal: успешный пост = тот же сигнал, что и успешная валидация. Полностью
    # «чистим» cred (is_valid=True, счётчик ошибок 0, cooldown/ошибки сняты), как в
    # validate-success. Так transient/флапнувший доступ, на котором реально удалось
    # разместить, возвращается в пул как valid.
    cred_values: dict = {
        "amount_use": WpCredential.amount_use + 1,
        "last_used_at": now,
        "is_valid": True,
        "error_counter": 0,
        "error_cooldown_until": None,
        "last_error_at": None,
        "last_error_message": None,
        "last_validation_kind": "ok",
        "last_validated_at": now,
    }
    if outcome.posted_via == "admin":
        cred_values["can_admin_login"] = True
        cred_values["can_post_via_admin"] = True
    else:
        # xmlrpc (или legacy без posted_via — считаем xmlrpc)
        cred_values["can_xmlrpc"] = True
        cred_values["can_post_via_xmlrpc"] = True
    await session.execute(
        update(WpCredential).where(WpCredential.id == credential.id).values(**cred_values)
    )
    # Успешный пост ЛЮБЫМ каналом → сбрасываем site-failure счётчик (сайт жив).
    # last_working_url обновляем только при xmlrpc (для admin это не URL endpoint-а).
    # last_used_at → для LRU-отбора в _pick_candidate_sites (ровный делёж пула).
    site_values: dict = {
        "consecutive_site_failures": 0, "last_working_at": now, "last_used_at": now,
    }
    if outcome.working_xmlrpc_url:
        site_values["last_working_url"] = outcome.working_xmlrpc_url
    await session.execute(
        update(WpSite).where(WpSite.id == site.id).values(**site_values)
    )

    session.add(
        ProjectWpUsed(
            project_id=project_id,
            site_id=site.id,
            posting_run_id=run_id,
            text_item_id=item.id,
            credential_id=credential.id,
        )
    )
    await session.commit()


async def _mark_text_failed(
    session: AsyncSession, *, item_id: int, error_message: str
) -> None:
    await session.execute(
        update(TextItem)
        .where(TextItem.id == item_id)
        .values(
            status=TextItemStatus.FAILED.value,
            attempts=TextItem.attempts + 1,
            last_error=error_message[:1000],
        )
    )
    await session.commit()


async def _release_text_back_to_pending(
    session: AsyncSession, *, item_id: int, error_message: str | None = None
) -> None:
    """Вернуть text_item в pending (например, не нашли свободный сайт)."""
    values: dict = {
        "status": TextItemStatus.PENDING.value,
        "attempts": TextItem.attempts + 1,
    }
    if error_message:
        values["last_error"] = error_message[:1000]
    await session.execute(update(TextItem).where(TextItem.id == item_id).values(**values))
    await session.commit()


async def _mark_credential_invalid(
    session: AsyncSession, *, credential_id: int, reason: str
) -> None:
    await session.execute(
        update(WpCredential)
        .where(WpCredential.id == credential_id)
        .values(is_valid=False)
    )
    await session.commit()
    log.warning("posting.credential.invalidated", credential_id=credential_id, reason=reason)


async def _bump_credential_error(session: AsyncSession, credential_id: int) -> int:
    """Инкремент error_counter, вернуть новое значение."""
    res = await session.execute(
        update(WpCredential)
        .where(WpCredential.id == credential_id)
        .values(error_counter=WpCredential.error_counter + 1)
        .returning(WpCredential.error_counter)
    )
    await session.commit()
    val = res.scalar_one_or_none()
    return int(val or 0)


async def _reset_site_url(session: AsyncSession, site_id: int) -> None:
    """Сбросить кеш discovery — следующий пост запустит discovery заново."""
    await session.execute(
        update(WpSite).where(WpSite.id == site_id).values(last_working_url=None)
    )
    await session.commit()


# При достижении этого порога подряд идущих site-class fail-ов — авто-выключаем
# сайт. Совпадает с константой в domain/wp_batches/service.py — должны совпадать
# чтобы batch-валидатор и posting-worker вели себя одинаково.
_SITE_FAILURE_DISABLE_THRESHOLD = 10


async def _bump_site_failure(
    session: AsyncSession,
    site_id: int,
    *,
    kind: str,
    disable_threshold: int = DEFAULT_SITE_DISABLE_THRESHOLD,
    disable_threshold_cf: int = DEFAULT_SITE_DISABLE_THRESHOLD_CF,
) -> None:
    """
    Site-class fail (network/server_error/xmlrpc_disabled/timeout/CF/broken_endpoint):
      - инкрементим consecutive_site_failures
      - «мягкий» порог (_SITE_FAILURE_DISABLE_THRESHOLD=10): выключаем ТОЛЬКО
        если нет рабочего cred (как раньше)
      - безусловный порог (`disable_threshold`; для CF — `disable_threshold_cf`):
        выключаем ВСЕГДА, даже с valid-cred. Закрывает дыру, из-за которой
        стабильно мёртвый для постинга сайт (CF/network/server_error) жил вечно,
        пока у него был рабочий логин (socialmonk с 66 CF-фейлами).

    Любой OK или auth-fail (т.е. сайт ответил XML-RPC-ом) сбрасывает счётчик
    в 0 — это происходит в `_mark_text_posted` и в AUTH_INVALID branch.
    """
    now = datetime.now(UTC)
    site = await session.scalar(select(WpSite).where(WpSite.id == site_id))
    if site is None:
        return
    new_count = (site.consecutive_site_failures or 0) + 1
    values: dict = {
        "consecutive_site_failures": new_count,
        "last_site_failure_at": now,
        "last_site_failure_kind": kind,
    }
    # CF отдельно агрессивнее — сайт под Cloudflare почти не «оживает», а каждый
    # headful-фейл стоит ~30 сек.
    uncond = disable_threshold_cf if kind == "cf_challenge" else disable_threshold
    if site.is_active and new_count >= max(1, uncond):
        # Безусловное выключение — valid-cred больше не «защищает» мёртвый сайт.
        values["is_active"] = False
        values["auto_disabled_at"] = now
        log.warning(
            "posting.site.auto_disabled_unconditional",
            site_id=site_id, domain=site.domain,
            failures=new_count, last_kind=kind, threshold=uncond,
        )
    elif new_count >= _SITE_FAILURE_DISABLE_THRESHOLD and site.is_active:
        # Per-cred guard: на «мягком» пороге не выключаем домен пока у него есть
        # подтверждённо рабочий cred (cred_status='valid') — transient-блипы не
        # убивают полезный сайт раньше времени.
        has_valid = await session.scalar(
            select(WpCredential.id).where(
                WpCredential.site_id == site_id,
                WpCredential.deleted_at.is_(None),
                WpCredential.cred_status == "valid",
            ).limit(1)
        )
        if has_valid is None:
            values["is_active"] = False
            values["auto_disabled_at"] = now
            log.warning(
                "posting.site.auto_disabled",
                site_id=site_id, domain=site.domain,
                failures=new_count, last_kind=kind,
            )
        else:
            log.info(
                "posting.site.disable_skipped_has_valid_cred",
                site_id=site_id, domain=site.domain, failures=new_count,
            )
    await session.execute(update(WpSite).where(WpSite.id == site_id).values(**values))
    await session.commit()


async def _disable_site_instant(
    session: AsyncSession, site_id: int, *, reason: str
) -> None:
    """Парковки/мусор: 1 ответа достаточно чтобы выключить сайт навсегда
    (см. _SITE_INSTANT_DISABLE_KINDS в batch validator)."""
    now = datetime.now(UTC)
    await session.execute(
        update(WpSite).where(WpSite.id == site_id).values(
            is_active=False,
            consecutive_site_failures=_SITE_FAILURE_DISABLE_THRESHOLD,
            last_site_failure_at=now,
            last_site_failure_kind=reason,
            auto_disabled_at=now,
        )
    )
    await session.commit()


# ─── Heartbeat / control polling ──────────────────────────────────────


async def _heartbeat_loop(run_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with WriteSession() as s:
                await s.execute(
                    update(PostingRun)
                    .where(PostingRun.id == run_id)
                    .values(worker_heartbeat_at=datetime.now(UTC))
                )
                await s.commit()
        except Exception as e:
            log.warning("posting.heartbeat.error", run_id=run_id, error=str(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def _read_control_flags(run_id: int) -> tuple[bool, bool]:
    """Вернуть (pause_requested, cancel_requested)."""
    async with WriteSession() as s:
        res = await s.execute(
            select(PostingRun.pause_requested, PostingRun.cancel_requested).where(
                PostingRun.id == run_id
            )
        )
        row = res.one_or_none()
        if row is None:
            return (False, True)  # run исчез — считаем cancel
        return bool(row[0]), bool(row[1])


# ─── Proxy pool resolution & rotation ──────────────────────────────────


async def _resolve_proxy_pool(
    session: AsyncSession, selector: str | None, fallback_proxy_id: int | None,
) -> list[str | None]:
    """По селектору и/или fallback proxy_id вернуть список proxy-URL (или [None]
    для direct); worker делает random rotation per request. Общая логика вынесена
    в domain.proxies.service.resolve_proxy_pool (её же использует link-check)."""
    from domain.proxies.service import resolve_proxy_pool
    return await resolve_proxy_pool(session, selector, fallback_proxy_id)


class HttpxClientPool:
    """
    Ленивый кеш httpx.AsyncClient — один клиент на уникальный proxy URL.
    `pick_random()` отдаёт клиент с рандомным proxy из пула.

    Каждый клиент имеет свой connection pool — что важно для производительности:
    повторные запросы через тот же proxy переиспользуют TLS-сессию.
    """

    def __init__(
        self,
        proxy_urls: list[str | None],
        *,
        timeout_seconds: int,
        verify: bool = False,
    ):
        # Дедупликация на случай если разные proxy_id указывают на один URL
        self._proxy_urls: list[str | None] = list({u: None for u in (proxy_urls or [None])}.keys())
        self._timeout = timeout_seconds
        self._verify = verify
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def __len__(self) -> int:
        return len(self._proxy_urls)

    def _get_or_create(self, proxy_url: str | None) -> httpx.AsyncClient:
        if proxy_url not in self._clients:
            self._clients[proxy_url] = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                proxy=proxy_url,
                verify=self._verify,
            )
        return self._clients[proxy_url]

    def pick_random(self) -> tuple[httpx.AsyncClient, str | None]:
        """Случайный клиент + его proxy_url (для логов)."""
        proxy_url = random.choice(self._proxy_urls)
        return (self._get_or_create(proxy_url), proxy_url)

    async def aclose(self) -> None:
        await asyncio.gather(
            *(c.aclose() for c in self._clients.values()),
            return_exceptions=True,
        )
        self._clients.clear()


# ─── Tier-based posting chain ──────────────────────────────────────────


# Какие ErrorKind считаем основанием для fallback на Tier 2 (wp-admin).
# XMLRPC_DISABLED — XML-RPC просто выключен, может wp-admin живой.
# BROKEN_ENDPOINT — endpoint существует но отдаёт мусор, тоже стоит попробовать
# admin как альтернативный канал. NETWORK / TASK_TIMEOUT не считаем — там
# сайт скорее всего совсем недоступен, Tier 2 тоже отлетит.
_TIER2_FALLBACK_KINDS: set = set()


def _build_tier2_fallback_kinds():
    """Lazy — заполняется при первом вызове (ErrorKind берётся из wp_client)."""
    global _TIER2_FALLBACK_KINDS
    if _TIER2_FALLBACK_KINDS:
        return _TIER2_FALLBACK_KINDS
    from infrastructure.wp_client import ErrorKind as EK
    # XML-RPC отвалился неоднозначно → стоит попробовать admin как
    # альтернативный канал. SERVER_ERROR/UNKNOWN добавлены: xmlrpc.php мог
    # отдать 5xx/мусор, но wp-admin живой (частый кейс — плагин режет
    # именно xmlrpc, не весь сайт). NETWORK/TIMEOUT не включаем — там сайт
    # скорее всего недоступен целиком.
    _TIER2_FALLBACK_KINDS = {
        EK.XMLRPC_DISABLED, EK.BROKEN_ENDPOINT, EK.SERVER_ERROR, EK.UNKNOWN,
    }
    return _TIER2_FALLBACK_KINDS


def _cf_site_root(site: WpSite) -> str:
    """Корень сайта (scheme+host) для браузер-логина — НЕ xmlrpc-урл."""
    from urllib.parse import urlparse
    if site.last_working_url:
        p = urlparse(site.last_working_url)
        if p.netloc:
            return f"{p.scheme or 'https'}://{p.netloc}"
    return f"https://{site.domain}"


async def _post_via_cf_browser(
    *, site: WpSite, cred: WpCredential, title: str, content: str,
    proxy_url: str | None,
) -> PostOutcome | None:
    """CF Tier 3 постинг: переиспользуем кешированную браузер-сессию (curl_cffi
    replay через wp-admin classic-форму). Сессии нет/протухла → логинимся
    браузером (тот же proxy/IP) и кешируем. Возвращает PostOutcome, либо None
    если браузер недоступен (тогда caller падает в обычную цепочку)."""
    from infrastructure.cf_browser import (
        browser_login_session, get_cached_session, is_browser_available,
        post_via_session,
    )
    from infrastructure.wp_client import ErrorKind as _EK
    if not is_browser_available():
        return None
    base = _cf_site_root(site)
    from urllib.parse import urlparse
    host = urlparse(base).netloc
    pw = decrypt_password(cred.password)

    async def _login() -> dict | None:
        # concurrency=None → переиспользуем сем, спраймленный на старте run-а.
        return await browser_login_session(base, cred.login, pw, proxy_url=proxy_url)

    session = await get_cached_session(host, proxy_url)
    if not session:
        session = await _login()
    if not session:
        return PostOutcome(error=_EK.CF_CHALLENGE,
                           error_message="cf browser login failed")
    res = await post_via_session(base, session, title=title, content=content,
                                 proxy_url=proxy_url)
    if res.get("status") == "expired":
        # Куки протухли → перелогин и один ретрай.
        session = await _login()
        if not session:
            return PostOutcome(error=_EK.CF_CHALLENGE,
                               error_message="cf re-login failed")
        res = await post_via_session(base, session, title=title, content=content,
                                     proxy_url=proxy_url)
    if res.get("status") == "ok":
        # Запостили браузер-сессией → сайт точно CF. Метим cf_protected (если
        # ещё нет), чтобы следующие посты шли сразу в Tier 3 без request-first.
        if not getattr(site, "cf_protected", False):
            try:
                async with WriteSession() as s:
                    await s.execute(update(WpSite).where(WpSite.id == site.id)
                                    .values(cf_protected=True))
                    await s.commit()
                site.cf_protected = True
            except Exception as e:  # noqa: BLE001
                log.debug("posting.cf_mark.failed", site_id=site.id, error=str(e))
        return PostOutcome(error=_EK.OK, post_id=res.get("post_id"),
                           posted_url=res.get("posted_url"), posted_via="admin")
    return PostOutcome(error=_EK.CF_CHALLENGE,
                       error_message=f"cf post: {res.get('error', 'failed')}"[:200])


async def _post_with_method_chain(
    *,
    method: str,
    poster: XmlRpcPoster,
    http: httpx.AsyncClient,
    site: WpSite,
    cred: WpCredential,
    title: str,
    content: str,
    post_date: datetime,
    timeout_seconds: int,
    proxy_url: str | None = None,
) -> PostOutcome:
    """
    Цепочка попыток в зависимости от run.posting_method.

    Tier 3 (CF/Patchright) встроен здесь: cf_protected-сайты идут в браузер-сессию
    СРАЗУ (минуя request-first), а на CF_CHALLENGE из обычной цепочки — как
    fallback. См. _post_via_cf_browser.
    """
    from infrastructure.wp_admin_client import post_via_admin
    from infrastructure.wp_client import ErrorKind as _EK

    cred_password = decrypt_password(cred.password)

    async def _try_xmlrpc() -> PostOutcome:
        return await poster.post(
            site=site, login=cred.login, password=cred_password,
            title=title, content=content, post_date=post_date,
        )

    async def _try_admin() -> PostOutcome:
        return await post_via_admin(
            http,
            site=site, login=cred.login, password=cred_password,
            title=title, content=content, timeout_seconds=timeout_seconds,
            proxy_url=proxy_url,
        )

    # CF-сайт (помечен на валидации браузером) → сразу Tier 3 (cached-session
    # replay), request-first бесполезен: CF режет и xmlrpc, и httpx-admin.
    # Браузер недоступен (None) → падаем в обычную цепочку как fallback.
    if getattr(site, "cf_protected", False):
        cf_outcome = await _post_via_cf_browser(
            site=site, cred=cred, title=title, content=content, proxy_url=proxy_url,
        )
        if cf_outcome is not None:
            return cf_outcome

    if method == "xmlrpc_only":
        outcome = await _try_xmlrpc()
        if outcome.success:
            outcome.posted_via = "xmlrpc"
    elif method == "admin_only":
        outcome = await _try_admin()
        if outcome.success:
            outcome.posted_via = "admin"
    else:
        # auto: XML-RPC сначала, при «канал мёртв» — Tier 2 (admin)
        outcome = await _try_xmlrpc()
        if outcome.success:
            outcome.posted_via = "xmlrpc"
        elif outcome.error in _build_tier2_fallback_kinds():
            log.info(
                "posting.tier2.fallback",
                site_id=site.id, cred_id=cred.id,
                tier1_kind=outcome.error.value,
            )
            outcome = await _try_admin()
            if outcome.success:
                outcome.posted_via = "admin"

    # Tier 3 (Patchright): на CF_CHALLENGE постим через браузер-сессию (один
    # раз логинимся браузером → curl_cffi replay). Помечаем сайт cf_protected
    # выше по стеку (см. _post_one_item) — следующие посты сразу пойдут в Tier 3.
    if outcome.error == _EK.CF_CHALLENGE:
        cf_outcome = await _post_via_cf_browser(
            site=site, cred=cred, title=title, content=content, proxy_url=proxy_url,
        )
        if cf_outcome is not None:
            return cf_outcome
    return outcome


# ─── Постинг одного text_item ─────────────────────────────────────────


def _compute_post_date(publish_from, publish_to, now: datetime) -> datetime:
    """Дата публикации для WP-поста по окну (app_settings publish_from/to).

    Окно — инструмент *back-date*: пост получает прошедшую дату, поэтому сразу
    опубликован (не Scheduled), виден анониму и «падает вниз» в ленте. Верхнюю
    границу клампим к `now` — НИКОГДА не уходим в будущее (будущая post_date →
    WP ставит пост в Scheduled и публично прячет до даты).

    - оба None            → now (публикуем текущим моментом);
    - окно в прошлом      → случайная дата внутри [start, end];
    - окно кончается позже → случайная дата внутри [start, now];
    - окно целиком впереди → now.
    """
    if publish_from is None or publish_to is None:
        return now
    window_start = datetime.combine(publish_from, datetime.min.time(), UTC)
    window_end = datetime.combine(publish_to, datetime.max.time(), UTC)
    effective_end = min(window_end, now)
    if window_start >= effective_end:
        return now
    span = (effective_end - window_start).total_seconds()
    return window_start + timedelta(seconds=random.uniform(0, span))


async def _post_one_item(
    *,
    item: TextItem,
    run: PostingRun,
    poster: XmlRpcPoster,
    client_pool: HttpxClientPool,
    registry: SiteClaimRegistry,
    semaphore: asyncio.Semaphore,
    global_limit: int = 80,
    site_disable: tuple[int, int] = (
        DEFAULT_SITE_DISABLE_THRESHOLD,
        DEFAULT_SITE_DISABLE_THRESHOLD_CF,
    ),
) -> None:
    async with semaphore:
        # 1. Загрузить тело текста: из texts (B1) с fallback на MinIO
        try:
            from domain.texts import read_item_body

            async with WriteSession() as s:
                content = await read_item_body(
                    s, text_id=item.text_id, storage_key=item.storage_key)
        except Exception as e:
            log.exception("posting.item.download_failed", item_id=item.id, error=str(e))
            async with WriteSession() as s:
                await _mark_text_failed(s, item_id=item.id, error_message=f"download: {e}")
                await _bump_run_counter(s, run.id, "failed_count")
            return

        title = item.title or "Untitled"
        # Окно публикации задаётся super_admin-ом через app_settings
        # (publish_from/publish_to). Если оба NULL — публикуем текущим моментом.
        # Иначе — случайный datetime внутри окна. Цель окна — *back-date*: пост
        # получает прошедшую дату, поэтому сразу опубликован (не Scheduled),
        # виден анониму и «падает вниз» в ленте. Поэтому верхнюю границу клампим
        # к now — НИКОГДА не планируем в будущее (будущая post_date → WP ставит
        # пост в Scheduled и публично прячет до даты).
        now_ts = datetime.now(UTC)
        post_date = _compute_post_date(run.publish_from, run.publish_to, now_ts)

        # 2. Подбирать сайты пока не запостим / не кончатся
        tried_sites: set[int] = set()
        attempts_budget = 10  # глобальный потолок попыток per item, защита от петель

        while attempts_budget > 0:
            attempts_budget -= 1

            _f_langs, _f_tlds, _f_tags, _f_domains = _run_site_filter(run)
            async with WriteSession() as s:
                candidates = await _pick_candidate_sites(
                    s,
                    project_id=run.project_id,
                    run_id=run.id,
                    exclude_site_ids=tried_sites
                    | {sid for sid in registry._exhausted},
                    limit=5,
                    site_langs=_f_langs,
                    site_tlds=_f_tlds,
                    site_tags=_f_tags,
                    site_domains=_f_domains,
                )

            if not candidates:
                # Сайтов больше нет — вернём text_item в pending. Финальное решение
                # (need_more_admins vs done) примет основной цикл при следующем
                # _pick_candidate_sites — если опять пусто, run помечается.
                async with WriteSession() as s:
                    await _release_text_back_to_pending(
                        s, item_id=item.id, error_message="no candidate sites"
                    )
                return

            posted = False
            for site in candidates:
                if registry.is_exhausted(site.id):
                    tried_sites.add(site.id)
                    continue

                async with registry.claim(site.id):
                    # Повторно проверим что site всё ещё имеет квоту в проекте —
                    # могла успеть другая корутина запостить и забить лимит.
                    async with WriteSession() as s:
                        already = await s.scalar(
                            select(func.count(ProjectWpUsed.id)).where(
                                ProjectWpUsed.project_id == run.project_id,
                                ProjectWpUsed.site_id == site.id,
                            )
                        ) or 0
                        max_reuse = await s.scalar(
                            select(PostingRun.max_posts_per_site).where(
                                PostingRun.id == run.id
                            )
                        ) or 1
                        if already >= max_reuse:
                            tried_sites.add(site.id)
                            continue

                        # Свежий site со всеми credentials
                        fresh_site = await s.scalar(
                            select(WpSite)
                            .options(selectinload(WpSite.credentials))
                            .where(WpSite.id == site.id)
                        )

                    if fresh_site is None:
                        tried_sites.add(site.id)
                        continue

                    # Берём только подтверждённо рабочие cred (cred_status='valid'),
                    # не сырой is_valid — согласовано с candidate-selection.
                    valid_creds = [
                        c for c in fresh_site.credentials
                        if c.cred_status == "valid" and c.deleted_at is None
                    ][:MAX_CREDS_PER_SITE]

                    if not valid_creds:
                        registry.mark_exhausted(site.id)
                        tried_sites.add(site.id)
                        continue

                    site_done = False
                    for cred in valid_creds:
                        import time as _t
                        _t0 = _t.monotonic()
                        # Proxy rotation: на каждый post берём random клиент из
                        # пула. Это распределяет нагрузку по 2550 webshare прокси
                        # вместо одного IP который мгновенно бы бакнули плагины.
                        http_for_post, picked_proxy = client_pool.pick_random()
                        rotated_poster = XmlRpcPoster(
                            http_for_post, timeout_seconds=run.timeout_seconds,
                            proxy_url=picked_proxy,
                        )
                        # Канал постинга — определяется run.posting_method:
                        method = getattr(run, "posting_method", "auto") or "auto"
                        # Глобальный слот: общий потолок на ВСЕ run-ы/процессы.
                        # Items разных прогонов чередуются здесь → «всё двигается
                        # понемногу» без приоритета. crash-safe (ZSET в Redis).
                        try:
                            async with posting_limiter.slot(limit=global_limit):
                                outcome = await _post_with_method_chain(
                                    method=method,
                                    poster=rotated_poster,
                                    http=http_for_post,  # шарим httpx сессию для cookies (Tier 2)
                                    site=fresh_site,
                                    cred=cred,
                                    title=title,
                                    content=content,
                                    post_date=post_date,
                                    timeout_seconds=run.timeout_seconds,
                                    proxy_url=picked_proxy,  # тот же exit для curl_cffi-fallback (Tier 2)
                                )
                        except Exception as _post_exc:
                            # Обрыв соединения / неожиданная сетевая ошибка во время
                            # постинга («Server disconnected …»). НЕ валим айтем —
                            # это site-class transient: бампим site-failure (cooldown),
                            # помечаем сайт exhausted на этот run и перескакиваем на
                            # ДРУГОЙ сайт (= другой доступ). Айтем остаётся pending.
                            log.warning(
                                "posting.item.transient_exception",
                                run_id=run.id, item_id=item.id, site_id=site.id,
                                cred_id=cred.id, error=str(_post_exc)[:200],
                            )
                            async with WriteSession() as s:
                                await _bump_site_failure(
                                    s, site.id, kind="network",
                                    disable_threshold=site_disable[0],
                                    disable_threshold_cf=site_disable[1])
                                if picked_proxy:
                                    from domain.proxies.service import (
                                        report_proxy_failure_by_url,
                                    )
                                    await report_proxy_failure_by_url(s, picked_proxy)
                            registry.mark_exhausted(site.id)
                            site_done = True
                            break  # → следующий сайт (другой доступ)
                        log.debug(
                            "posting.item.attempt",
                            run_id=run.id, item_id=item.id, site_id=site.id,
                            cred_id=cred.id, proxy=picked_proxy[:50] if picked_proxy else "direct",
                            outcome=outcome.error.value,
                        )
                        gym_posting_xmlrpc_duration_seconds.observe(_t.monotonic() - _t0)
                        gym_posting_xmlrpc_requests_total.labels(outcome=outcome.error.value).inc()

                        if outcome.success:
                            verify_mode = getattr(run, "post_verify", "mark") or "mark"
                            do_verify = _is_post_type(run) and outcome.posted_url
                            target = item.target_domain or item.link_url or ""
                            # AUTO: подтверждаем ссылку ДО зачёта поста. Нет ссылки →
                            # этот сайт «съел» бэклинк → исключаем и хопаем на другой.
                            if do_verify and verify_mode == "auto":
                                from domain.postings.verify import verify_with_retries
                                vfound, vresolved = await verify_with_retries(
                                    outcome.posted_url, target,
                                    proxy_url=picked_proxy, attempts=3, delay=3.0)
                                if not vfound:
                                    async with WriteSession() as s:
                                        await s.execute(update(TextItem)
                                            .where(TextItem.id == item.id)
                                            .values(link_verified=False,
                                                    verify_attempts=TextItem.verify_attempts + 1))
                                        await s.commit()
                                    log.info("posting.item.verify_failed_hop",
                                             run_id=run.id, item_id=item.id, site_id=site.id,
                                             post_url=outcome.posted_url)
                                    registry.mark_exhausted(site.id)
                                    tried_sites.add(site.id)
                                    site_done = True
                                    break  # → следующий сайт
                                outcome.posted_url = vresolved  # резолвленный permalink

                            async with WriteSession() as s:
                                await _mark_text_posted(
                                    s,
                                    item=item,
                                    site=fresh_site,
                                    credential=cred,
                                    outcome=outcome,
                                    project_id=run.project_id,
                                    run_id=run.id,
                                )
                                if do_verify and verify_mode == "auto":
                                    await s.execute(update(TextItem)
                                        .where(TextItem.id == item.id)
                                        .values(link_verified=True, verified_at=datetime.now(UTC),
                                                verify_attempts=TextItem.verify_attempts + 1))
                                await _bump_run_counter(s, run.id, "posted_count")
                            # MARK: пост уже зачтён — отдельной проверкой ставим ✓/✗ + резолв URL
                            if do_verify and verify_mode == "mark":
                                from domain.postings.verify import verify_post_link
                                vfound, vresolved = await verify_post_link(
                                    outcome.posted_url, target, proxy_url=picked_proxy)
                                async with WriteSession() as s:
                                    await _annotate_post_verify(s, item.id, vfound, vresolved)
                            log.info(
                                "posting.item.posted",
                                run_id=run.id,
                                item_id=item.id,
                                site_id=site.id,
                                credential_id=cred.id,
                                url=outcome.posted_url,
                                verified=(verify_mode if do_verify else None),
                            )
                            posted = True
                            site_done = True
                            break

                        # Ошибка — реагируем по категории.
                        # Append-only event log (site_events) — одна запись на
                        # любой posting-failure, для аналитики/истории сайта.
                        if outcome.error and outcome.error != ErrorKind.OK:
                            async with WriteSession() as s_ev:
                                from domain.site_events import record_site_event
                                await record_site_event(
                                    s_ev,
                                    site_id=site.id,
                                    credential_id=cred.id,
                                    source="posting",
                                    error_kind=outcome.error.value,
                                    error_message=outcome.error_message,
                                    posting_run_id=run.id,
                                    proxy_id=None,
                                )
                                await s_ev.commit()
                        # ──────────────────────────────────────────────────────
                        # Eager DB-marking: каждый ответ от WP обновляет capability
                        # флаги. Это позволяет следующим runs/валидаторам не дёргать
                        # заведомо мёртвые каналы.
                        if outcome.error in DEFINITIVE_CRED_INVALID_KINDS:
                            async with WriteSession() as s:
                                # Channel info: XML-RPC живой, но cred не работает
                                await s.execute(
                                    update(WpCredential)
                                    .where(WpCredential.id == cred.id)
                                    .values(
                                        can_xmlrpc=True,
                                        can_post_via_xmlrpc=False,
                                        last_validation_kind=outcome.error.value,
                                        last_error_message=(outcome.error_message or "")[:500],
                                        last_validated_at=datetime.now(UTC),
                                    )
                                )
                                # Site-failure-счётчик сбрасываем — сайт ответил XML-RPC-ом,
                                # значит он жив.
                                await s.execute(
                                    update(WpSite).where(WpSite.id == site.id)
                                    .values(consecutive_site_failures=0)
                                )
                                new_errs = await _bump_credential_error(s, cred.id)
                                if new_errs >= CREDENTIAL_ERROR_THRESHOLD:
                                    await _mark_credential_invalid(
                                        s,
                                        credential_id=cred.id,
                                        reason=str(outcome.error),
                                    )
                            # пробуем следующую credential этого же сайта
                            continue

                        if outcome.error == ErrorKind.XMLRPC_DISABLED:
                            # Channel info: XML-RPC мёртв на этом сайте.
                            # cred всё ещё может работать через wp-admin (Tier 2),
                            # но в текущем posting flow (XML-RPC only) — нет.
                            async with WriteSession() as s:
                                await s.execute(
                                    update(WpCredential).where(WpCredential.id == cred.id)
                                    .values(
                                        can_xmlrpc=False,
                                        can_post_via_xmlrpc=False,
                                        last_validation_kind=outcome.error.value,
                                        last_error_message=(outcome.error_message or "")[:500],
                                        last_validated_at=datetime.now(UTC),
                                    )
                                )
                                await _reset_site_url(s, site.id)
                                # Bump site failure counter (XMLRPC_DISABLED — site-class
                                # error, не cred). После 10 → авто-выключение сайта.
                                await _bump_site_failure(
                                    s, site.id, kind="xmlrpc_disabled",
                                    disable_threshold=site_disable[0],
                                    disable_threshold_cf=site_disable[1])
                            registry.mark_exhausted(site.id)
                            site_done = True
                            break

                        if outcome.error == ErrorKind.SITE_NOT_FOUND:
                            async with WriteSession() as s:
                                await _reset_site_url(s, site.id)
                                await _bump_site_failure(
                                    s, site.id, kind="site_not_found",
                                    disable_threshold=site_disable[0],
                                    disable_threshold_cf=site_disable[1])
                            registry.mark_exhausted(site.id)
                            site_done = True
                            break

                        if outcome.error == ErrorKind.PARKED:
                            # Сайт мёртв (parking / suspended / cgi-sys) —
                            # instant disable, не тратим 10 ретраев.
                            async with WriteSession() as s:
                                await _disable_site_instant(
                                    s, site.id, reason=ErrorKind.PARKED.value,
                                )
                            registry.mark_exhausted(site.id)
                            site_done = True
                            log.warning(
                                "posting.site.auto_disabled_parked",
                                site_id=site.id, domain=site.domain,
                            )
                            break

                        if outcome.error == ErrorKind.RATE_LIMITED:
                            # 429: сайт/прокси перегружены ПРЯМО СЕЙЧАС — это НЕ мёртвый
                            # сайт. Помечаем exhausted на ЭТОТ прогон (остальные потоки
                            # пропустят его и разъедутся по другим сайтам, без 429-шторма),
                            # но НЕ бьём по счётчику авто-выключения (времянка — в следующем
                            # прогоне сайт снова доступен).
                            log.info(
                                "posting.item.rate_limited",
                                run_id=run.id, item_id=item.id, site_id=site.id,
                                cred_id=cred.id,
                            )
                            registry.mark_exhausted(site.id)
                            site_done = True
                            break

                        if outcome.error in (
                            ErrorKind.NETWORK,
                            ErrorKind.SERVER_ERROR,
                            ErrorKind.TASK_TIMEOUT,
                            ErrorKind.CF_CHALLENGE,
                            ErrorKind.BROKEN_ENDPOINT,
                            ErrorKind.UNKNOWN,
                        ):
                            log.warning(
                                "posting.item.transient_fail",
                                site_id=site.id,
                                credential_id=cred.id,
                                error=outcome.error.value,
                                msg=outcome.error_message,
                            )
                            async with WriteSession() as s:
                                # CF_CHALLENGE: пометим site.cf_protected=True для UI
                                if outcome.error == ErrorKind.CF_CHALLENGE:
                                    await s.execute(
                                        update(WpSite).where(WpSite.id == site.id)
                                        .values(cf_protected=True)
                                    )
                                # Bump site counter для всех transient site-class kinds
                                await _bump_site_failure(
                                    s, site.id, kind=outcome.error.value,
                                    disable_threshold=site_disable[0],
                                    disable_threshold_cf=site_disable[1])
                                # Bump proxy failure только для тех ошибок что явно
                                # указывают на проблему сети/прокси, не сайта.
                                # NETWORK + TASK_TIMEOUT = подозрение на proxy hang.
                                # SERVER_ERROR/CF/BROKEN — это про сайт, не прокси.
                                if outcome.error in (ErrorKind.NETWORK, ErrorKind.TASK_TIMEOUT) and picked_proxy:
                                    from domain.proxies.service import report_proxy_failure_by_url
                                    await report_proxy_failure_by_url(s, picked_proxy)
                            # Сайт может ожить позже — exhaust только на этот run
                            registry.mark_exhausted(site.id)
                            site_done = True
                            break

                    tried_sites.add(site.id)
                    if posted:
                        return
                    if site_done:
                        # Перейти к следующему сайту в текущем batch candidates
                        continue

            if posted:
                return
            # никто из текущей пачки не сработал — запросим следующую

        # Бюджет попыток исчерпан
        log.warning("posting.item.attempts_exhausted", item_id=item.id, run_id=run.id)
        async with WriteSession() as s:
            await _mark_text_failed(
                s, item_id=item.id, error_message="no site succeeded after retries"
            )
            await _bump_run_counter(s, run.id, "failed_count")


# ─── Link-run loop (sitewide / homepage) ─────────────────────────────


async def _run_link_async(run_id: int, concurrency: int) -> dict:
    """Простановка сквозных/homepage ссылок. site_id НЕ привязан к айтему —
    process_link_item крутит кандидатов из пула, пока не разместит (как постинг)."""
    log_ctx = log.bind(run_id=run_id, kind="link")
    from domain.wp_links import process_link_item

    stop_event = asyncio.Event()
    # admin-логин + probe + verify — тяжелее постинга, кап конкуренси ниже
    sem = asyncio.Semaphore(max(1, min(concurrency, 6)))
    global_limit = await _read_global_posting_limit()
    heartbeat = asyncio.create_task(_heartbeat_loop(run_id, stop_event))
    final_status = PostingRunStatus.DONE
    rearm_at: datetime | None = None  # drip-feed: момент следующей порции (→ scheduled)
    # Общий registry занятых/перепробованных сайтов прогона — координация
    # конкуренси, чтобы параллельные айтемы не целили в один сайт.
    used_sites: set[int] = set()
    # Пул прокси задачи (proxy_selector) — резолвим один раз, дальше random per attempt.
    async with WriteSession() as s:
        _run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        proxy_urls = await _resolve_proxy_pool(
            s, getattr(_run, "proxy_selector", None), getattr(_run, "proxy_id", None))

    async def _one(item_id: int):
        async with sem, posting_limiter.slot(limit=global_limit):
            try:
                res = await process_link_item(
                    item_id, used_sites=used_sites, actor_id=None, proxy_urls=proxy_urls)
            except Exception as e:
                log_ctx.warning("link.item.exception", item_id=item_id, error=str(e))
                res = {"status": "error"}
            field = {"placed": "posted_count",
                     "skip_exists": "skipped_count"}.get(res.get("status"), "failed_count")
            async with WriteSession() as s:
                await _bump_run_counter(s, run_id, field)

    try:
        while True:
            paused, cancelled = await _read_control_flags(run_id)
            if cancelled:
                final_status = PostingRunStatus.CANCELLED
                break
            if paused:
                async with WriteSession() as s:
                    await s.execute(update(PostingRun).where(PostingRun.id == run_id)
                                    .values(status=PostingRunStatus.PAUSED.value))
                    await s.commit()
                await publish_run_event(run_id, "status", {"status": PostingRunStatus.PAUSED.value})
                return {"ok": True, "status": PostingRunStatus.PAUSED.value}

            async with WriteSession() as s:
                batch = await _pick_pending_batch(s, run_id, BATCH_SIZE)
            if not batch:
                # Drip-feed: остались pending с будущим not_before? Не финишируем —
                # перевзводим run в scheduled на момент ближайшей порции, cron
                # dispatch_scheduled_runs поднимет его снова (не держим worker-слот).
                async with WriteSession() as s:
                    next_due = await s.scalar(
                        select(func.min(TextItem.not_before)).where(
                            TextItem.posting_run_id == run_id,
                            TextItem.status == TextItemStatus.PENDING.value,
                            TextItem.not_before.isnot(None),
                            TextItem.not_before > datetime.now(UTC),
                        ))
                if next_due is not None:
                    final_status = PostingRunStatus.SCHEDULED
                    rearm_at = next_due
                    log_ctx.info("link.drip.rearm", resume_at=next_due.isoformat())
                break
            await asyncio.gather(*[_one(it.id) for it in batch])
    finally:
        stop_event.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass

    async with WriteSession() as s:
        await _reconcile_run_counters(s, run_id)
        if final_status == PostingRunStatus.SCHEDULED:
            # drip re-arm: не финишируем, спим до следующей порции (cron поднимет)
            await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(
                status=PostingRunStatus.SCHEDULED.value, scheduled_for=rearm_at))
        else:
            await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(
                status=final_status.value, finished_at=datetime.now(UTC)))
        await s.commit()
    await publish_run_event(run_id, "status", {"status": final_status.value})
    log_ctx.info("link.run.done", status=final_status.value)
    return {"ok": True, "status": final_status.value}


# ─── Главный цикл прогона ─────────────────────────────────────────────


async def _run_posting_async(run_id: int) -> dict:
    log_ctx = log.bind(run_id=run_id)
    log_ctx.info("posting.start")

    # Загрузить run, перевести в running
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            log_ctx.error("posting.run_not_found")
            return {"ok": False, "error": "run not found"}
        if run.status not in (
            PostingRunStatus.QUEUED.value,
            PostingRunStatus.PAUSED.value,
        ):
            log_ctx.warning("posting.wrong_status", status=run.status)
            return {"ok": False, "error": f"wrong status: {run.status}"}

        await s.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(
                status=PostingRunStatus.RUNNING.value,
                started_at=run.started_at or datetime.now(UTC),
                worker_heartbeat_at=datetime.now(UTC),
                pause_requested=False,
            )
        )
        await s.commit()
        # Перечитываем — нам нужны актуальные поля
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        assert run is not None
        # CF Tier 3: праймим браузер-семафор один раз на run (из AppSettings).
        # Дальше _post_via_cf_browser дёргает browser_login_session(concurrency=None)
        # и переиспользует этот сем.
        try:
            from domain.app_settings.service import get_app_settings
            from infrastructure.cf_browser.client import _semaphore as _cf_sem
            _cf_sem((await get_app_settings(s)).cf_browser_concurrency)
        except Exception as e:  # noqa: BLE001
            log_ctx.debug("posting.cf_sem_prime.failed", error=str(e))
    await publish_run_event(run_id, "status", {"status": PostingRunStatus.RUNNING.value})

    # Link-типы (sitewide_link / homepage_link) — отдельный путь: site_id у айтема
    # НЕ задан, process_link_item крутит кандидатов пула (как постинг), пока не
    # разместит через admin + verify, тогда и пишет site_id/результат.
    if run.task_type in (RunTaskType.SITEWIDE_LINK.value, RunTaskType.HOMEPAGE_LINK.value):
        return await _run_link_async(run_id, run.concurrency)

    stop_event = asyncio.Event()
    # Тюнинг из app_settings (live): global-потолок, floor для fair-share, пороги
    # авто-выключения. conc_ceiling = run.concurrency — максимум разгона ОДНОГО
    # прогона; реальный размер окна адаптивно меньше при многих активных прогонах.
    global_limit, conc_floor, sd_general, sd_cf = await _read_posting_tuning()
    site_disable = (sd_general, sd_cf)
    conc_ceiling = max(1, run.concurrency)
    semaphore = asyncio.Semaphore(conc_ceiling)
    registry = SiteClaimRegistry()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(run_id, stop_event))

    final_status: PostingRunStatus = PostingRunStatus.DONE
    final_error: str | None = None
    rearm_at: datetime | None = None  # drip-feed: момент следующей порции (→ scheduled)
    # In-flight задачи скользящего окна (объявлено до try — чтобы finally мог
    # подчистить, если упадём до/во время цикла).
    inflight: set[asyncio.Task] = set()

    # Резолвим proxy-пул: list of URLs (или [None] для direct).
    # Worker будет делать random rotation per text_item — иначе все 1000+
    # постов пошли бы через 1 IP и плагины рейт-лимитили бы нас.
    async with WriteSession() as s_px:
        proxy_urls = await _resolve_proxy_pool(
            s_px,
            selector=getattr(run, "proxy_selector", None),
            fallback_proxy_id=run.proxy_id,
        )
    log_ctx.info(
        "posting.proxy.pool_resolved",
        selector=getattr(run, "proxy_selector", None),
        proxy_id=run.proxy_id,
        pool_size=len(proxy_urls),
        direct=(proxy_urls == [None]),
    )
    client_pool = HttpxClientPool(proxy_urls, timeout_seconds=run.timeout_seconds)

    # Стриминг (Фаза 2): auto csv_campaign с несгенерированными айтемами —
    # запускаем генерацию ПАРАЛЛЕЛЬНО (та же celery-task/loop, отдельная корутина);
    # постинг забирает готовые айтемы (text-гейт), не дожидаясь всех текстов.
    gen_task: asyncio.Task | None = None
    if run.content_source == "csv_campaign" and run.run_mode == "auto":
        # Drip (Фаза 3): размазать not_before по spread_days → генерация и постинг
        # идут порциями по дням (идемпотентно). Затем стрим: генерим «созревшие»
        # в горизонте, постинг забирает готовые, остальное cron поднимет позже.
        if (run.spread_days or 0) > 0:
            from domain.content_engine.campaign import apply_drip_not_before
            await apply_drip_not_before(run_id, run.spread_days, run.scheduled_for)
        async with WriteSession() as s_g:
            ungen = await s_g.scalar(select(func.count(TextItem.id)).where(
                TextItem.posting_run_id == run_id, TextItem.text_id.is_(None),
                TextItem.status == TextItemStatus.PENDING.value))
        if ungen and ungen > 0:
            from domain.content_engine import generate_run_items
            gen_task = asyncio.create_task(generate_run_items(run_id, finalize=True))
            log_ctx.info("posting.stream.gen_started", ungenerated=int(ungen))

    try:
        # Для текущего scope (_post_one_item принимает один poster) собираем
        # default poster — он используется как "stable" reference, но в реальности
        # _post_with_method_chain дёргает client_pool.pick_random() per request.
        async with httpx.AsyncClient(
            timeout=run.timeout_seconds,
            follow_redirects=True,
            proxy=proxy_urls[0] if proxy_urls else None,
            verify=False,
        ) as http_client:
            poster = XmlRpcPoster(
                http_client, timeout_seconds=run.timeout_seconds,
                proxy_url=proxy_urls[0] if proxy_urls else None,
            )

            # ── Стриминг + адаптивная конкурентность (work-conserving) ──
            # Скользящее окно: держим в полёте `target` айтемов и подтягиваем
            # новый pending как только слот освободился — без барьера батча.
            # target = clamp(global // активные_прогоны, floor, ceiling),
            # пересчитывается раз в TARGET_RECALC_S. Глобальный Redis-лимитер
            # внутри _post_one_item остаётся жёстким cross-run потолком.
            gen_wait_stalls = 0       # стрим: подряд-итераций ожидания без прогресса генерации
            last_ext_ungen = -1
            target = conc_ceiling
            last_target_at = 0.0
            need_more_admins = False
            last_progress_at = time.monotonic()
            last_posted_seen = await _read_posted_count(run_id)

            while True:
                # Control flags
                paused, cancelled = await _read_control_flags(run_id)
                if cancelled:
                    final_status = PostingRunStatus.CANCELLED
                    log_ctx.info("posting.cancelled")
                    if inflight:
                        await asyncio.gather(*inflight, return_exceptions=True)
                    break
                if paused:
                    # Дождаться текущих in-flight, чтобы не бросить их в POSTING.
                    if inflight:
                        await asyncio.gather(*inflight, return_exceptions=True)
                        inflight.clear()
                    async with WriteSession() as s:
                        await s.execute(
                            update(PostingRun)
                            .where(PostingRun.id == run_id)
                            .values(status=PostingRunStatus.PAUSED.value)
                        )
                        await s.commit()
                    log_ctx.info("posting.paused")
                    final_status = PostingRunStatus.PAUSED
                    final_error = None
                    return {"ok": True, "status": PostingRunStatus.PAUSED.value}

                # Пересчёт адаптивного размера окна (раз в TARGET_RECALC_S).
                now_mono = time.monotonic()
                if now_mono - last_target_at >= TARGET_RECALC_S:
                    target = await _effective_concurrency(
                        global_limit=global_limit, ceiling=conc_ceiling, floor=conc_floor)
                    last_target_at = now_mono

                # Refill: добиваем окно до target свежими pending (только с готовым
                # текстом — пустые gen-айтемы пропускаем, их сгенерят отдельно).
                if not need_more_admins and len(inflight) < target:
                    need = target - len(inflight)
                    async with WriteSession() as s:
                        batch = await _pick_pending_batch(s, run_id, need, require_text=True)
                    if batch:
                        # Перед запуском проверим, что вообще есть куда постить.
                        _f_langs, _f_tlds, _f_tags, _f_domains = _run_site_filter(run)
                        async with WriteSession() as s:
                            any_site = await _pick_candidate_sites(
                                s, project_id=run.project_id, run_id=run.id,
                                exclude_site_ids=set(), limit=1,
                                site_langs=_f_langs, site_tlds=_f_tlds, site_tags=_f_tags, site_domains=_f_domains,
                            )
                        if not any_site:
                            # Вернуть забранные в pending; финализируем в
                            # need_more_admins, когда сольётся in-flight.
                            async with WriteSession() as s:
                                await s.execute(
                                    update(TextItem)
                                    .where(
                                        TextItem.id.in_([t.id for t in batch]),
                                        TextItem.status == TextItemStatus.POSTING.value,
                                    )
                                    .values(status=TextItemStatus.PENDING.value)
                                )
                                await s.commit()
                            need_more_admins = True
                        else:
                            for item in batch:
                                t = asyncio.create_task(_post_one_item(
                                    item=item, run=run, poster=poster,
                                    client_pool=client_pool, registry=registry,
                                    semaphore=semaphore, global_limit=global_limit,
                                    site_disable=site_disable,
                                ))
                                inflight.add(t)

                # Ничего в полёте — разбираемся с финальным состоянием.
                if not inflight:
                    if need_more_admins:
                        final_status = PostingRunStatus.NEED_MORE_ADMINS
                        log_ctx.warning("posting.no_more_sites")
                        break
                    # Стриминг (auto): генерация ещё идёт параллельно (своя
                    # корутина) — ждём новые готовые айтемы, не финишируем рано.
                    if gen_task is not None and not gen_task.done():
                        await asyncio.sleep(2)
                        continue
                    # Стриминг (manual): «Старт постинга» поверх идущей «Сгенерировать
                    # тексты» — внешняя генерация ещё наполняет тексты. Stall-кап
                    # ~10 мин без прогресса (на случай SIGKILL генератора).
                    ext_ungen = await _external_gen_pending(run_id)
                    if ext_ungen > 0:
                        gen_wait_stalls = (gen_wait_stalls + 1
                                           if 0 <= last_ext_ungen <= ext_ungen else 0)
                        last_ext_ungen = ext_ungen
                        if gen_wait_stalls <= 300:
                            await asyncio.sleep(2)
                            continue
                        log_ctx.warning("posting.ext_gen_stalled", ungenerated=ext_ungen)
                    # Drip-feed: остались pending с будущим not_before? Перевзводим
                    # run в scheduled на момент ближайшей порции (cron поднимет).
                    async with WriteSession() as s:
                        next_due = await s.scalar(
                            select(func.min(TextItem.not_before)).where(
                                TextItem.posting_run_id == run_id,
                                TextItem.status == TextItemStatus.PENDING.value,
                                TextItem.not_before.isnot(None),
                                TextItem.not_before > datetime.now(UTC),
                            )
                        )
                    if next_due is not None:
                        final_status = PostingRunStatus.SCHEDULED
                        rearm_at = next_due
                        log_ctx.info("posting.drip.rearm", resume_at=next_due.isoformat())
                        break
                    # Остались задачи needs_review (нужны данные)? Run не «done».
                    async with WriteSession() as s:
                        nr = await s.scalar(
                            select(func.count(TextItem.id)).where(
                                TextItem.posting_run_id == run_id,
                                TextItem.status == TextItemStatus.NEEDS_REVIEW.value,
                            )
                        )
                    if nr and nr > 0:
                        final_status = PostingRunStatus.NEEDS_REVIEW
                        log_ctx.info("posting.needs_review_remaining", count=int(nr))
                        break
                    log_ctx.info("posting.no_pending_left")
                    break

                # Ждём завершения хотя бы одного айтема — как слот освободился, на
                # следующей итерации сразу подтянем новый pending (continuous).
                done, _pend = await asyncio.wait(
                    inflight, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    inflight.discard(t)
                    if t.cancelled():
                        continue
                    exc = t.exception()
                    if exc is not None:
                        # _post_one_item сам помечает item failed на штатных ошибках;
                        # сюда долетает только неожиданное исключение корутины.
                        log_ctx.exception("posting.item.unexpected", error=str(exc))

                # No-progress детектор. ВАЖНО: НЕ сбрасываем registry._exhausted по
                # таймеру — иначе 25 слотов снова и снова возвращаются на одни и те же
                # мёртвые низкие id и не доходят до здоровых сайтов пула. Сбрасываем
                # ТОЛЬКО когда реально кончились СВЕЖИЕ (не-exhausted) сайты — как
                # ретрай transient; и если глобально сайтов нет совсем → need_more_admins.
                now_mono = time.monotonic()
                if now_mono - last_progress_at >= NO_PROGRESS_CHECK_S:
                    cur_posted = await _read_posted_count(run_id)
                    if cur_posted > last_posted_seen:
                        last_posted_seen = cur_posted
                    else:
                        _f_langs, _f_tlds, _f_tags, _f_domains = _run_site_filter(run)
                        async with WriteSession() as s:
                            fresh = await _pick_candidate_sites(
                                s, project_id=run.project_id, run_id=run.id,
                                exclude_site_ids=set(registry._exhausted), limit=1,
                                site_langs=_f_langs, site_tlds=_f_tlds, site_tags=_f_tags, site_domains=_f_domains,
                            )
                        if not fresh:
                            # весь eligible-пул уже перепробован этим run-ом — ретраим
                            # (вдруг transient ожил); если и глобально пусто → нужны
                            # новые доступы.
                            registry._exhausted.clear()
                            async with WriteSession() as s:
                                any_global = await _pick_candidate_sites(
                                    s, project_id=run.project_id, run_id=run.id,
                                    exclude_site_ids=set(), limit=1,
                                    site_langs=_f_langs, site_tlds=_f_tlds, site_tags=_f_tags, site_domains=_f_domains,
                                )
                            if not any_global:
                                need_more_admins = True
                                log_ctx.warning("posting.no_progress.no_more_sites")
                    last_progress_at = now_mono

    except Exception as e:
        log_ctx.exception("posting.unexpected_error", error=str(e))
        final_status = PostingRunStatus.FAILED
        final_error = str(e)[:500]

    finally:
        stop_event.set()
        # Подчистка скользящего окна: отменяем не завершённые in-flight задачи и
        # ждём их (иначе при падении цикла останутся «висеть» после закрытия
        # http-клиентов). Items, застрявшие в POSTING, ниже метятся как FAILED.
        if inflight:
            for t in inflight:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
            inflight.clear()
        # Стриминг: дождаться/остановить параллельную генерацию
        if gen_task is not None:
            if not gen_task.done():
                gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(heartbeat_task, timeout=2)
        except (asyncio.TimeoutError, Exception):
            pass
        # Закрываем все httpx-клиенты из пула (TLS connections)
        try:
            await client_pool.aclose()
        except Exception as e:
            log_ctx.warning("posting.proxy_pool.close_failed", error=str(e))

    # Финализация
    async with WriteSession() as s:
        # привести счётчики к фактическим терминальным статусам (убрать раздув ретраями)
        await _reconcile_run_counters(s, run_id)
        # drip-feed re-arm: run не закончен — спит до следующей порции (scheduled),
        # finished_at не ставим, прописываем scheduled_for=rearm_at (cron поднимет).
        if final_status == PostingRunStatus.SCHEDULED:
            final_values = {
                "status": PostingRunStatus.SCHEDULED.value,
                "scheduled_for": rearm_at,
                "finished_at": None,
                "worker_heartbeat_at": datetime.now(UTC),
            }
        elif final_status == PostingRunStatus.NEEDS_REVIEW:
            # run «приостановлен» до дозаполнения needs_review-задач — не finished
            final_values = {
                "status": PostingRunStatus.NEEDS_REVIEW.value,
                "finished_at": None,
                "worker_heartbeat_at": datetime.now(UTC),
            }
        else:
            final_values = {
                "status": final_status.value,
                "finished_at": datetime.now(UTC),
                "worker_heartbeat_at": datetime.now(UTC),
            }
        await s.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(**final_values)
        )
        # Если run упал/прерван/отменён — items, застрявшие в POSTING, помечаем
        # как FAILED с описательным сообщением. Иначе они остаются в "posting"
        # навсегда (для UI это «зависли», для воркера — потеряны).
        # Юзер увидит честный counter "N failed" и кликнет Restart.
        if final_status in (
            PostingRunStatus.FAILED,
            PostingRunStatus.CANCELLED,
            PostingRunStatus.INTERRUPTED,
        ):
            stuck_msg = (
                f"run {final_status.value}: " + (final_error or "worker crashed mid-flight")
            )[:500]
            reset = await s.execute(
                update(TextItem)
                .where(
                    TextItem.posting_run_id == run_id,
                    TextItem.status == TextItemStatus.POSTING.value,
                )
                .values(
                    status=TextItemStatus.FAILED.value,
                    last_error=stuck_msg,
                )
            )
            stuck_count = int(reset.rowcount or 0)
            if stuck_count > 0:
                # Обновляем failed_count чтобы он отражал реальность
                await s.execute(
                    update(PostingRun)
                    .where(PostingRun.id == run_id)
                    .values(failed_count=PostingRun.failed_count + stuck_count)
                )
                log_ctx.warning(
                    "posting.stuck_items_marked_failed",
                    stuck_count=stuck_count, run_status=final_status.value,
                )
        await s.commit()
    await publish_run_event(
        run_id, "status", {"status": final_status.value, "error": final_error}
    )

    log_ctx.info("posting.done", status=final_status.value, error=final_error)
    return {"ok": final_status == PostingRunStatus.DONE, "status": final_status.value}


async def _read_posted_count(run_id: int) -> int:
    async with WriteSession() as s:
        res = await s.scalar(
            select(PostingRun.posted_count).where(PostingRun.id == run_id)
        )
        return int(res or 0)


# ─── Per-item постинг/репост (по кнопке из UI, вне общего run-цикла) ───


async def _post_one_item_standalone(item_id: int, *, is_repost: bool) -> dict:
    """Постинг/репост ОДНОГО айтема по кнопке. Переиспользует _post_one_item
    (post-тип) / process_link_item (link-тип) — ту же логику hopping, что и общий
    цикл, только собираем shared-объекты на один айтем.

    Репост: айтем уже POSTED («пост не отобразился») — сбрасываем результат,
    ИСКЛЮЧАЕМ текущий сайт и берём новый (съедает ещё один слот сайта)."""
    await write_engine.dispose()
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        if item is None:
            return {"ok": False, "status": "not_found"}
        run = await s.scalar(select(PostingRun).where(PostingRun.id == item.posting_run_id))
        if run is None:
            return {"ok": False, "status": "run_not_found"}
        is_link = run.task_type in (
            RunTaskType.SITEWIDE_LINK.value, RunTaskType.HOMEPAGE_LINK.value)
        old_site_id = item.site_id
        # Guard статуса
        if is_repost:
            if item.status != TextItemStatus.POSTED.value:
                return {"ok": False, "status": "not_posted"}
        else:
            if item.status not in (
                TextItemStatus.PENDING.value, TextItemStatus.FAILED.value):
                return {"ok": False, "status": f"wrong_status:{item.status}"}
            if not is_link and item.text_id is None and item.storage_key is None:
                return {"ok": False, "status": "no_text"}
        # Claim → POSTING; для репоста сбрасываем прошлый результат
        vals: dict = {"status": TextItemStatus.POSTING.value, "last_error": None}
        if is_repost:
            vals.update({
                "posted_url": None, "post_id": None, "posted_at": None,
                "site_id": None, "credential_id": None, "placed_via": None,
                "placement_ref": None, "verified_at": None, "verified_urls": None,
            })
        await s.execute(update(TextItem).where(TextItem.id == item_id).values(**vals))
        await s.commit()
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))

    async with WriteSession() as s_px:
        proxy_urls = await _resolve_proxy_pool(
            s_px, getattr(run, "proxy_selector", None), run.proxy_id)

    # Link-тип → process_link_item (self-contained); репост исключает старый сайт
    if is_link:
        from domain.wp_links import process_link_item
        used = {old_site_id} if (is_repost and old_site_id) else set()
        return await process_link_item(item_id, used_sites=used, proxy_urls=proxy_urls)

    # Post-тип → _post_one_item с собранными shared-объектами на один айтем
    registry = SiteClaimRegistry()
    if is_repost and old_site_id:
        registry.mark_exhausted(old_site_id)  # старый сайт исключаем из подбора
    client_pool = HttpxClientPool(proxy_urls, timeout_seconds=run.timeout_seconds)
    global_limit = await _read_global_posting_limit()
    sem = asyncio.Semaphore(1)
    async with httpx.AsyncClient(
        timeout=run.timeout_seconds, follow_redirects=True,
        proxy=proxy_urls[0] if proxy_urls else None, verify=False,
    ) as http_client:
        poster = XmlRpcPoster(
            http_client, timeout_seconds=run.timeout_seconds,
            proxy_url=proxy_urls[0] if proxy_urls else None)
        await _post_one_item(
            item=item, run=run, poster=poster, client_pool=client_pool,
            registry=registry, semaphore=sem, global_limit=global_limit)
    async with WriteSession() as s:
        st = await s.scalar(select(TextItem.status).where(TextItem.id == item_id))
    return {"ok": st == TextItemStatus.POSTED.value, "status": st, "item_id": item_id}


# ─── Celery entry point ───────────────────────────────────────────────


@celery_app.task(name="postings.run_posting", bind=True)
def run_posting(self, run_id: int) -> dict:
    """
    Celery wrapper над async loop. Один воркер берёт один run за раз
    (worker_prefetch_multiplier=1 в celery_app).

    ВАЖНО: каждый вызов = новый asyncio loop. SQLAlchemy async engine кеширует
    asyncpg-коннекты, привязанные к loop, в котором их создали. Если этот loop
    закрылся (предыдущая task завершилась), оставшиеся коннекты бьют с
    "got Future attached to a different loop". Поэтому dispose() в начале каждой
    task — снимаем pool, новые коннекты создадутся в текущем loop.
    """
    return asyncio.run(_run_posting_entry(run_id, self.request.id))


@celery_app.task(name="postings.post_one_item")
def post_one_item(item_id: int, is_repost: bool = False) -> dict:
    """Celery-обёртка: постинг/репост одного айтема по кнопке из UI."""
    return asyncio.run(_post_one_item_standalone(item_id, is_repost=is_repost))


async def _run_posting_entry(run_id: int, task_id: str) -> dict:
    await write_engine.dispose()
    await _set_celery_task_id(run_id, task_id)
    return await _run_posting_async(run_id)


async def _set_celery_task_id(run_id: int, task_id: str) -> None:
    async with WriteSession() as s:
        await s.execute(
            update(PostingRun)
            .where(PostingRun.id == run_id)
            .values(celery_task_id=task_id)
        )
        await s.commit()
