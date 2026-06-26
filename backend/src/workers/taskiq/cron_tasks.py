"""
TaskIQ периодические задачи (через `schedule` label).

- `dispatch_scheduled_runs` — раз в минуту: ищет runs со status='scheduled' и
  `scheduled_for <= now()`, переводит в `queued` и отправляет в Celery.
- `recover_stalled_runs` — раз в минуту: ищет runs со status='running' и
  устаревшим worker_heartbeat_at (> HEARTBEAT_STALE_S), помечает как
  `interrupted`, сбрасывает их text_items posting→pending. Resume из UI
  поднимет run в queued и продолжит с того места.
- `gc_tmp_uploads` — раз в час: удаляет объекты из MinIO bucket
  `uploads-tmp` старше TMP_UPLOAD_TTL_HOURS.

Запуск scheduler-а:
    taskiq scheduler workers.taskiq:scheduler
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import or_, select, update

from core.config import settings
from core.db import WriteSession
from core.storage import StorageError, storage
from core.taskiq_app import broker
from infrastructure.db.models import (
    CELERY_PRIORITY_MAP,
    PostingRun,
    PostingRunStatus,
    TextItem,
    TextItemStatus,
)

log = structlog.get_logger(__name__)

# Если последний heartbeat был > 90 секунд назад при concurrency-вьюхе — считаем
# воркер мёртвым (heartbeat пишется каждые 10 секунд).
HEARTBEAT_STALE_S = 90

# Сколько часов держим в uploads-tmp недокачанные/осиротевшие архивы перед GC.
# 24h с запасом — даже большие zip распаковываются за минуты.
TMP_UPLOAD_TTL_HOURS = 24


@broker.task(
    task_name="postings.dispatch_scheduled_runs",
    schedule=[{"cron": "* * * * *"}],
)
async def dispatch_scheduled_runs() -> dict:
    """Перевести scheduled→queued runs у которых пришло время + enqueue Celery."""
    now = datetime.now(UTC)
    dispatched: list[int] = []

    runs_with_prio: list[tuple[int, str]] = []
    async with WriteSession() as s:
        rows = (
            await s.execute(
                select(PostingRun.id, PostingRun.priority).where(
                    PostingRun.status == PostingRunStatus.SCHEDULED.value,
                    PostingRun.deleted_at.is_(None),
                    or_(
                        PostingRun.scheduled_for.is_(None),
                        PostingRun.scheduled_for <= now,
                    ),
                )
            )
        ).all()
        runs_with_prio = [(int(r[0]), str(r[1] or "normal")) for r in rows]

        if not runs_with_prio:
            return {"ok": True, "dispatched": 0}

        await s.execute(
            update(PostingRun)
            .where(PostingRun.id.in_([rid for rid, _ in runs_with_prio]))
            .values(status=PostingRunStatus.QUEUED.value)
        )
        await s.commit()
        dispatched = [rid for rid, _ in runs_with_prio]

    # Enqueue в Celery вне SQL-сессии
    from core.celery_app import celery_app

    for rid, prio_name in runs_with_prio:
        try:
            celery_app.send_task(
                "postings.run_posting",
                args=[rid],
                priority=CELERY_PRIORITY_MAP.get(prio_name, 5),
            )
        except Exception as e:
            log.warning("scheduler.celery_enqueue_failed", run_id=rid, error=str(e))

    log.info("scheduler.dispatched", count=len(dispatched), ids=dispatched)
    return {"ok": True, "dispatched": len(dispatched)}


@broker.task(
    task_name="postings.recover_stalled_runs",
    schedule=[{"cron": "* * * * *"}],
)
async def recover_stalled_runs() -> dict:
    """
    Найти runs со status='running' и worker_heartbeat_at старше HEARTBEAT_STALE_S
    (heartbeat пишется каждые 10с — устаревший >90с значит воркер умер, обычно
    деплой/рестарт).

    АВТО-RESUME: вместо пометки 'interrupted' + ручного Restart — сразу возвращаем
    ран в очередь (status='queued' + повторный enqueue Celery), чтобы постинг
    продолжился сам после рестарта воркера. Залипшие в 'posting' text_items
    возвращаем в 'pending', иначе их никто не подберёт.

    Исключение: если на ране на момент смерти стоял pause/cancel_requested —
    НЕ воскрешаем (уважаем намерение пользователя), помечаем 'interrupted'.
    """
    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=HEARTBEAT_STALE_S)
    resume: list[tuple[int, str]] = []
    interrupted: list[int] = []

    async with WriteSession() as s:
        rows = (
            await s.execute(
                select(
                    PostingRun.id,
                    PostingRun.priority,
                    PostingRun.pause_requested,
                    PostingRun.cancel_requested,
                ).where(
                    PostingRun.status == PostingRunStatus.RUNNING.value,
                    PostingRun.deleted_at.is_(None),
                    or_(
                        PostingRun.worker_heartbeat_at.is_(None),
                        PostingRun.worker_heartbeat_at < threshold,
                    ),
                )
            )
        ).all()
        if not rows:
            return {"ok": True, "resumed": 0, "interrupted": 0}

        for rid, prio, paused, cancelled in rows:
            if paused or cancelled:
                interrupted.append(int(rid))
            else:
                resume.append((int(rid), str(prio or "normal")))
        resume_ids = [rid for rid, _ in resume]

        if interrupted:
            await s.execute(
                update(PostingRun)
                .where(PostingRun.id.in_(interrupted))
                .values(
                    status=PostingRunStatus.INTERRUPTED.value,
                    finished_at=now,
                )
            )
        if resume_ids:
            await s.execute(
                update(PostingRun)
                .where(PostingRun.id.in_(resume_ids))
                .values(
                    status=PostingRunStatus.QUEUED.value,
                    finished_at=None,
                )
            )
        # Залипшие text_items в статусе 'posting' (воркер начал, но не дописал)
        # — по ВСЕМ затронутым ранам (и resume, и interrupted) обратно в pending.
        reset_result = await s.execute(
            update(TextItem)
            .where(
                TextItem.posting_run_id.in_(resume_ids + interrupted),
                TextItem.status == TextItemStatus.POSTING.value,
            )
            .values(status=TextItemStatus.PENDING.value)
        )
        await s.commit()

    # Повторный enqueue вне SQL-сессии (как dispatch_scheduled_runs).
    if resume:
        from core.celery_app import celery_app

        for rid, prio_name in resume:
            try:
                celery_app.send_task(
                    "postings.run_posting",
                    args=[rid],
                    priority=CELERY_PRIORITY_MAP.get(prio_name, 5),
                )
            except Exception as e:
                log.warning("recover.celery_enqueue_failed", run_id=rid, error=str(e))

    log.warning(
        "scheduler.recovered_runs",
        resumed=resume_ids,
        interrupted=interrupted,
        text_items_reset=int(reset_result.rowcount or 0),
    )
    return {
        "ok": True,
        "resumed": len(resume),
        "interrupted": len(interrupted),
        "text_items_reset": int(reset_result.rowcount or 0),
    }


# Батчи heartbeat не пишут — застрявшую валидацию ловим по прогрессу: батч в
# 'validating', стартовал давно и за BATCH_STALE_S ни одна cred не получила свежий
# last_validated_at → воркер мёртв (деплой). Берём с запасом (10 мин): пул=5
# параллельно, даже CF-tier3 ~минута/cred — реального простоя 10 мин у живого
# воркера не бывает, так что ложно «живой» батч не дёрнем.
BATCH_STALE_S = 600


@broker.task(
    task_name="batches.recover_stalled_batches",
    schedule=[{"cron": "* * * * *"}],
)
async def recover_stalled_batches() -> dict:
    """
    Авто-resume осиротевшей валидации батча после смерти воркера (деплой).

    run_batch_validation отбивает повторный запуск гардом «already running» пока
    статус 'validating', поэтому сперва выводим батч из 'validating' (→ 'paused'),
    затем переочередь validate_batch_task(scope='all') — cooldown сам пропустит
    уже проверенных. provision_after=False: в авто-recovery НЕ создаём
    пользователей на сайтах без ведома оператора.
    """
    from infrastructure.db.models import WpBatchStatus, WpCredential, WpImportBatch

    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=BATCH_STALE_S)
    # «есть свежий прогресс» — хоть одна cred батча валидировалась после threshold
    recent_progress = (
        select(WpCredential.id)
        .where(
            WpCredential.import_batch_id == WpImportBatch.id,
            WpCredential.deleted_at.is_(None),
            WpCredential.last_validated_at.isnot(None),
            WpCredential.last_validated_at >= threshold,
        )
        .exists()
    )
    stale_ids: list[int] = []
    async with WriteSession() as s:
        rows = (
            await s.execute(
                select(WpImportBatch.id).where(
                    WpImportBatch.status == WpBatchStatus.VALIDATING.value,
                    WpImportBatch.deleted_at.is_(None),
                    WpImportBatch.pause_requested.is_(False),
                    or_(
                        WpImportBatch.validation_started_at.is_(None),
                        WpImportBatch.validation_started_at < threshold,
                    ),
                    ~recent_progress,
                )
            )
        ).scalars().all()
        stale_ids = [int(x) for x in rows]
        if not stale_ids:
            return {"ok": True, "recovered": 0}

        # Выводим из 'validating' (иначе гард в run_batch_validation отобьёт).
        await s.execute(
            update(WpImportBatch)
            .where(WpImportBatch.id.in_(stale_ids))
            .values(status=WpBatchStatus.PAUSED.value, pause_requested=False)
        )
        await s.commit()

    for bid in stale_ids:
        try:
            await validate_batch_task.kiq(
                batch_id=bid,
                scope="all",
                level="full",
                provision_after=False,
                concurrency=settings.DEFAULT_VALIDATION_CONCURRENCY,
            )
        except Exception as e:
            log.warning("recover.batch_enqueue_failed", batch_id=bid, error=str(e))

    log.warning("scheduler.recovered_batches", ids=stale_ids, count=len(stale_ids))
    return {"ok": True, "recovered": len(stale_ids)}


@broker.task(task_name="content.generate_campaign")
async def generate_campaign_task(run_id: int) -> dict:
    """Генерация csv_campaign-рана (отдельная полоса, не блокирует постинг)."""
    from domain.content_engine import generate_campaign_run
    return await generate_campaign_run(run_id)


@broker.task(task_name="content.generate_item")
async def generate_item_task(item_id: int, regenerate: bool = False) -> dict:
    """Пер-айтем (ре)генерация текста по кнопке из UI."""
    from domain.content_engine import generate_item
    return await generate_item(item_id, regenerate=regenerate)


@broker.task(task_name="content.generate_run_items")
async def generate_run_items_task(run_id: int) -> dict:
    """Bulk «Сгенерировать тексты» — наполнить предсозданные пустые айтемы."""
    from domain.content_engine import generate_run_items
    return await generate_run_items(run_id)


@broker.task(task_name="content.fill_run_spins")
async def fill_run_spins_task(run_id: int) -> dict:
    """Bulk «Заполнить спины» (manual gen_per_row) — расшить готовые оригиналы в
    спин-варианты без старта постинга. Ран остаётся READY."""
    from domain.content_engine import fill_campaign_spins
    return await fill_campaign_spins(run_id)


@broker.task(task_name="content.fill_spins", schedule=[{"cron": "*/5 * * * *"}])
async def fill_spins_task() -> dict:
    """Spin-воркер: раз в 5 минут добиваем spin_formula у reusable-текстов
    (под reuse). Если spin-модели нет — ничего не делает."""
    from domain.content_engine import fill_pending_spins
    return await fill_pending_spins(limit=100)


@broker.task(task_name="wp.validate_batch")
async def validate_batch_task(
    batch_id: int,
    scope: str = "all",
    concurrency: int = 5,
    proxy_id: int | None = None,
    detect_lang: bool = True,
    actor_id: int | None = None,
    level: str = "full",
    provision_after: bool = False,
    provision_role: str = "author",
) -> dict:
    """On-demand триггер валидации одного батча из UI."""
    from domain.wp_batches.service import run_batch_validation

    return await run_batch_validation(
        batch_id,
        scope=scope,
        concurrency=concurrency,
        proxy_id=proxy_id,
        detect_lang=detect_lang,
        actor_id=actor_id,
        level=level,
        provision_after=provision_after,
        provision_role=provision_role,
    )


@broker.task(task_name="wp.provision_batch")
async def provision_batch_task(
    batch_id: int,
    role: str = "author",
    concurrency: int = 4,
    actor_id: int | None = None,
) -> dict:
    """Создать наши author-аккаунты на всех сайтах батча, где их ещё нет."""
    from domain.wp_provision import run_batch_provision

    return await run_batch_provision(
        batch_id, role=role, concurrency=concurrency, actor_id=actor_id,
    )


@broker.task(task_name="wp.provision_bulk")
async def provision_bulk_task(
    role: str = "author",
    concurrency: int = 4,
    actor_id: int | None = None,
) -> dict:
    """Создать наши author-аккаунты на ВСЕХ подходящих сайтах, где их ещё нет."""
    from domain.wp_provision import run_bulk_provision

    return await run_bulk_provision(role=role, concurrency=concurrency, actor_id=actor_id)


@broker.task(task_name="wp.validate_ondemand")
async def validate_ondemand(scope: str = "all", actor_id: int | None = None) -> dict:
    """
    Legacy pool-wide валидатор (без батча) — для creds которые лежат в пуле
    без import_batch_id (импортированы старыми путями до /batches).
    UI на /wp-sites ещё дёргает.
    """
    from domain.wp_validation.service import run_validation

    return await run_validation(scope=scope, actor_id=actor_id)


@broker.task(
    task_name="metrics.refresh_gauges",
    schedule=[{"cron": "* * * * *"}],  # каждую минуту
)
async def refresh_prometheus_gauges() -> dict:
    """
    Обновляет Prometheus gauge-метрики из БД:
      - gym_posting_runs_active
      - gym_wp_credentials_valid
      - gym_proxies_active
    """
    from sqlalchemy import func as _func, select as _select

    from core.metrics import (
        gym_posting_runs_active,
        gym_proxies_active,
        gym_wp_credentials_valid,
    )
    from infrastructure.db.models import Proxy, WpCredential

    async with WriteSession() as s:
        active_runs = int((await s.execute(
            _select(_func.count(PostingRun.id)).where(
                PostingRun.deleted_at.is_(None),
                PostingRun.status.in_((
                    PostingRunStatus.UNPACKING.value,
                    PostingRunStatus.QUEUED.value,
                    PostingRunStatus.RUNNING.value,
                    PostingRunStatus.PAUSED.value,
                    PostingRunStatus.SCHEDULED.value,
                )),
            )
        )).scalar_one())
        # cred_status='valid' (единый источник), не сырое is_valid — иначе
        # gauge включает transient и расходится с UI.
        cred_valid = int((await s.execute(
            _select(_func.count(WpCredential.id)).where(
                WpCredential.deleted_at.is_(None),
                WpCredential.cred_status == "valid",
            )
        )).scalar_one())
        proxies_active = int((await s.execute(
            _select(_func.count(Proxy.id)).where(
                Proxy.is_active.is_(True),
                Proxy.status == "active",
            )
        )).scalar_one())

    gym_posting_runs_active.set(active_runs)
    gym_wp_credentials_valid.set(cred_valid)
    gym_proxies_active.set(proxies_active)
    return {"ok": True, "active_runs": active_runs, "cred_valid": cred_valid, "proxies_active": proxies_active}


@broker.task(
    task_name="postings.gc_tmp_uploads",
    schedule=[{"cron": "0 * * * *"}],  # каждый час, ровно
)
async def gc_tmp_uploads() -> dict:
    """
    Удаляет осиротевшие объекты из uploads-tmp (.zip-архивы, которые остались
    после неудачной загрузки или crash-а unpack-таски). Порог — TMP_UPLOAD_TTL_HOURS.

    Архив корректно завершённого run-а тоже лежит здесь как
    `source_archive_storage_key`; ничего, что мы можем потерять текущий source —
    у нас уже распакованные text_items в `text-items/...`. GC source-архива
    после успешного unpack — отдельная фича (TODO: stage 3).
    """
    threshold = datetime.now(UTC) - timedelta(hours=TMP_UPLOAD_TTL_HOURS)
    bucket = settings.MINIO_BUCKET_UPLOADS
    deleted = 0
    kept = 0
    errors = 0

    try:
        for obj in storage.list_prefix_meta(bucket, prefix=""):
            # MinIO возвращает last_modified в UTC. Если по какой-то причине
            # отсутствует — пропускаем (на новых объектах он всегда есть).
            last_mod = getattr(obj, "last_modified", None)
            if last_mod is None:
                kept += 1
                continue
            if last_mod >= threshold:
                kept += 1
                continue
            try:
                storage.delete(bucket, obj.object_name)
                deleted += 1
            except StorageError as e:
                log.warning("scheduler.gc.delete_failed", key=obj.object_name, error=str(e))
                errors += 1
    except StorageError as e:
        log.exception("scheduler.gc.list_failed", error=str(e))
        return {"ok": False, "error": str(e), "deleted": deleted, "kept": kept, "errors": errors}

    log.info(
        "scheduler.gc.done",
        bucket=bucket,
        deleted=deleted,
        kept=kept,
        errors=errors,
        ttl_hours=TMP_UPLOAD_TTL_HOURS,
    )
    return {"ok": True, "deleted": deleted, "kept": kept, "errors": errors}


@broker.task(
    task_name="wp.refresh_pool_summary_mv",
    schedule=[{"cron": "* * * * *"}],  # каждую минуту
)
async def refresh_pool_summary_mv_task() -> dict:
    """REFRESH wp_pool_summary_mv — держит summary-карточки /wp-sites дешёвыми
    при росте до 600k+ сайтов. Concurrently, не блокирует читателей."""
    from domain.wp_sites.service import refresh_pool_summary_mv

    async with WriteSession() as s:
        await refresh_pool_summary_mv(s)
    return {"ok": True}


@broker.task(
    task_name="proxies.recheck_all_daily",
    schedule=[{"cron": "0 4 * * *"}],  # каждый день в 04:00
)
async def recheck_all_proxies_daily() -> dict:
    """Daily health-recheck всего proxy-пула: оживших разлочиваем, мёртвых
    помечаем down. Держит пул в актуальном состоянии без ручного вмешательства."""
    from domain.proxies.service import recheck_all_proxies

    async with WriteSession() as s:
        res = await recheck_all_proxies(s, only_active=False, concurrency=10)
    log.info("proxies.daily_recheck.done", **res)
    return {"ok": True, **res}


@broker.task(
    task_name="wp.ensure_text_items_partition",
    schedule=[{"cron": "0 3 25 * *"}],  # 25-го числа каждого месяца, 03:00
)
async def ensure_text_items_partition() -> dict:
    """Пре-создаёт месячные партиции text_items на текущий+2 месяца вперёд.
    Без этого новые строки падали бы в DEFAULT-партицию (медленнее, мешает
    архивации). Идемпотентно — существующие партиции пропускает."""
    from datetime import date
    from sqlalchemy import text as _text

    from core.db import WriteSession

    # Берём «сегодня» из БД (в скриптах Date.now недоступен, но в воркере ок).
    async with WriteSession() as s:
        today = (await s.execute(_text("SELECT current_date"))).scalar_one()

    created = []
    async with WriteSession() as s:
        for offset in range(0, 3):  # текущий + 2 вперёд
            y = today.year + (today.month - 1 + offset) // 12
            m = (today.month - 1 + offset) % 12 + 1
            start = date(y, m, 1)
            end = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            name = f"text_items_{y}_{m:02d}"
            exists = (await s.execute(_text(
                "SELECT 1 FROM pg_class WHERE relname=:n"
            ), {"n": name})).first()
            if exists:
                continue
            await s.execute(_text(
                f"CREATE TABLE {name} PARTITION OF text_items "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            ))
            created.append(name)
        if created:
            await s.commit()
    log.info("text_items.partitions_ensured", created=created)
    return {"ok": True, "created": created}


@broker.task(
    task_name="wp.ensure_site_events_partition",
    schedule=[{"cron": "0 3 25 * *"}],  # 25-го каждого месяца, 03:00
)
async def ensure_site_events_partition() -> dict:
    """Пре-создаёт месячные партиции site_events на текущий+2 месяца.
    Идемпотентно. Иначе новые события падают в DEFAULT-партицию."""
    from datetime import date
    from sqlalchemy import text as _text

    from core.db import WriteSession

    async with WriteSession() as s:
        today = (await s.execute(_text("SELECT current_date"))).scalar_one()

    created = []
    async with WriteSession() as s:
        for offset in range(0, 3):
            y = today.year + (today.month - 1 + offset) // 12
            m = (today.month - 1 + offset) % 12 + 1
            start = date(y, m, 1)
            end = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            name = f"site_events_{y}_{m:02d}"
            exists = (await s.execute(_text("SELECT 1 FROM pg_class WHERE relname=:n"), {"n": name})).first()
            if exists:
                continue
            await s.execute(_text(
                f"CREATE TABLE {name} PARTITION OF site_events "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            ))
            created.append(name)
        if created:
            await s.commit()
    log.info("site_events.partitions_ensured", created=created)
    return {"ok": True, "created": created}
