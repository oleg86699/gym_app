"""
TaskIQ task: перепроверка проставленных бэклинков завершённого прогона.

Запускается ВРУЧНУЮ (кнопка на странице run) после завершения постинга.
Берёт posted-итемы, которые УЖЕ были валидны (link_verified=true), и заново
фетчит страницу каждого поста (`verify_with_retries` с CF-fallback), проверяя,
что бэклинк на target_domain всё ещё на месте. Обновляет link_verified/verified_at.

Состояние пишется в posting_runs.link_check_* — фронт поллит run detail и видит
прогресс, а глобальная очередь показывает активную проверку (фиолетовый тип).

Параллельность ограничена семафором (это сетевые запросы — грузят сервер, и их
видно в очереди как «чем занят сервер»).
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from core.db import WriteSession
from core.taskiq_app import broker
from domain.postings.verify import verify_with_retries
from domain.proxies.service import resolve_proxy_pool
from infrastructure.db.models import PostingRun, TextItem, TextItemStatus

log = structlog.get_logger(__name__)

# Сколько страниц перепроверяем параллельно. Скромно — это внешние GET-ы,
# грузят сеть/CPU; задача и так видна в очереди как занятость сервера.
LINK_CHECK_CONCURRENCY = 8
_ATTEMPTS = 2
_RETRY_DELAY = 2.0
_TIMEOUT = 25.0


async def _load_targets(run_id: int) -> list[tuple[int, str, str]]:
    """(id, posted_url, target_domain) уже-валидных размещений прогона."""
    async with WriteSession() as s:
        rows = (await s.execute(
            select(TextItem.id, TextItem.posted_url, TextItem.target_domain)
            .where(
                TextItem.posting_run_id == run_id,
                TextItem.status == TextItemStatus.POSTED.value,
                TextItem.link_verified.is_(True),
                TextItem.posted_url.isnot(None),
                TextItem.target_domain.isnot(None),
            )
            .order_by(TextItem.id.asc())
        )).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _check_one(
    run_id: int, item_id: int, posted_url: str, target_domain: str,
    sem: asyncio.Semaphore, pool: list[str | None],
) -> None:
    # Ротация прокси по пулу прогона — те же exit-IP, что и при постинге; на
    # сотнях ссылок direct-IP сервера ловил бы блок/429.
    proxy = random.choice(pool) if pool else None
    async with sem:
        try:
            found, resolved = await verify_with_retries(
                posted_url, target_domain, proxy_url=proxy,
                attempts=_ATTEMPTS, delay=_RETRY_DELAY, timeout=_TIMEOUT,
            )
        except Exception as e:  # сеть/таймаут — считаем ссылку непроверенной (False)
            log.debug("linkcheck.item_error", run_id=run_id, item_id=item_id, error=str(e))
            found, resolved = False, posted_url

    now = datetime.now(UTC)
    item_values: dict = {
        "link_verified": found,
        "verified_at": now,
        "verify_attempts": TextItem.verify_attempts + 1,
    }
    if found and resolved:
        item_values["posted_url"] = resolved
    async with WriteSession() as s:
        await s.execute(update(TextItem).where(TextItem.id == item_id).values(**item_values))
        await s.execute(
            update(PostingRun).where(PostingRun.id == run_id).values(
                link_check_done=PostingRun.link_check_done + 1,
                link_check_valid=PostingRun.link_check_valid + (1 if found else 0),
                last_progress_at=now,
            )
        )
        await s.commit()


@broker.task(task_name="postings.validate_links")
async def validate_run_links(run_id: int) -> dict:
    """Перепроверить уже-валидные бэклинки прогона."""
    log_ctx = log.bind(run_id=run_id)
    targets = await _load_targets(run_id)

    # Инициализируем состояние проверки (running, total, сброс счётчиков) и
    # резолвим пул прокси прогона (тот же селектор, что использовался при постинге).
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        pool = await resolve_proxy_pool(s, run.proxy_selector, run.proxy_id)
        await s.execute(
            update(PostingRun).where(PostingRun.id == run_id).values(
                link_check_status="running",
                link_check_total=len(targets),
                link_check_done=0,
                link_check_valid=0,
                link_check_at=datetime.now(UTC),
            )
        )
        await s.commit()

    log_ctx.info("linkcheck.start", total=len(targets),
                 proxies=len([p for p in pool if p]), via_proxy=any(pool))
    try:
        if targets:
            sem = asyncio.Semaphore(LINK_CHECK_CONCURRENCY)
            await asyncio.gather(*(
                _check_one(run_id, item_id, posted_url, target_domain, sem, pool)
                for (item_id, posted_url, target_domain) in targets
            ))
    finally:
        async with WriteSession() as s:
            await s.execute(
                update(PostingRun).where(PostingRun.id == run_id).values(
                    link_check_status="done",
                    link_check_at=datetime.now(UTC),
                )
            )
            await s.commit()

    # Финальные счётчики для лога/ответа.
    async with WriteSession() as s:
        row = (await s.execute(
            select(PostingRun.link_check_total, PostingRun.link_check_valid)
            .where(PostingRun.id == run_id)
        )).one_or_none()
    total = int(row[0]) if row else len(targets)
    valid = int(row[1]) if row else 0
    log_ctx.info("linkcheck.done", total=total, valid=valid)
    return {"ok": True, "run_id": run_id, "total": total, "valid": valid}
