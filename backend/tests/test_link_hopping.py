"""Перебор сайтов при простановке ссылок (как постинг, без бюджета).

create_link_run больше НЕ привязывает site_id при создании — его выбирает
process_link_item на Start, крутя кандидатов пула, пока не разместит или пока
сайты не кончатся. Сеть мокаем (_place_on_site / candidate_link_sites).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import domain.wp_links.service as wls
import pytest_asyncio
from domain.wp_links import create_link_run
from infrastructure.db.models import TextItem
from infrastructure.db.models.posting import RunTaskType, TextItemStatus
from sqlalchemy import select, text as sql


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_link_hop():
    """create_link_run коммитит в общую dev-БД (откат db_session не помогает) —
    подчищаем созданные тестом runs по уникальному имени после каждого теста,
    чтобы не засорять реальный список ранов (cascade снесёт text_items)."""
    yield
    from core.db import WriteSession
    async with WriteSession() as s:
        await s.execute(sql("DELETE FROM posting_runs WHERE name = 'LINK HOP'"))
        await s.commit()


async def _mk_run(s, *, count=1, max_sites=None):
    return await create_link_run(
        s, project=SimpleNamespace(id=3), creator=SimpleNamespace(id=None),
        name="LINK HOP", task_type=RunTaskType.SITEWIDE_LINK.value,
        links=[{"url": "https://nawal.mx/", "anchor": "Nawal", "count": count}],
        concurrency=2, timeout_seconds=40, max_sites=max_sites,
    )


async def _items(s, run_id):
    return (await s.execute(
        select(TextItem).where(TextItem.posting_run_id == run_id)
        .order_by(TextItem.id))).scalars().all()


async def test_candidate_link_sites_real_sql(db_session):
    """РЕАЛЬНЫЙ запрос (без мока) — ловит SQL-ошибки вроде
    «SELECT DISTINCT … ORDER BY random() must appear in select list».
    Проверяем: исполняется, отдаёт список, уважает limit/exclude/random."""
    s = db_session
    ids = await wls.candidate_link_sites(s, limit=10)
    assert isinstance(ids, list) and len(ids) <= 10
    assert all(isinstance(x, int) for x in ids)
    if ids:
        # exclude_site_ids реально исключает
        ex = {ids[0]}
        ids2 = await wls.candidate_link_sites(s, limit=10, exclude_site_ids=ex)
        assert ids[0] not in ids2
        # порядок случайный (DISTINCT/GROUP BY + ORDER BY random) — два вызова
        # на большом пуле почти всегда дают разный префикс
        a = await wls.candidate_link_sites(s)
        b = await wls.candidate_link_sites(s)
        assert sorted(a) == sorted(b)  # множество то же
        if len(a) > 5:
            assert a[:5] != b[:5] or a == b  # порядок крутится (мягко)


async def test_create_link_run_no_site_binding(db_session):
    """Айтемы создаются БЕЗ site_id, в нужном количестве, PENDING."""
    s = db_session
    run = await _mk_run(s, count=3)
    items = await _items(s, run.id)
    assert len(items) == 3
    assert all(it.site_id is None for it in items)
    assert all(it.link_url == "https://nawal.mx/" for it in items)
    assert all(it.status == TextItemStatus.PENDING.value for it in items)
    # content_hash уникальны (url|anchor|run|seq) → не схлопываются в один айтем
    assert len({it.content_hash for it in items}) == 3


async def test_create_link_run_max_sites_cap(db_session):
    """max_sites ограничивает общее число размещений (айтемов)."""
    s = db_session
    run = await _mk_run(s, count=5, max_sites=2)
    assert len(await _items(s, run.id)) == 2


async def test_create_link_run_scheduled_proxy_drip(db_session):
    """scheduled_for → статус SCHEDULED; proxy_selector сохраняется;
    spread_days → not_before размазан в будущее (drip)."""
    from datetime import UTC, datetime, timedelta

    from infrastructure.db.models.posting import PostingRun, PostingRunStatus

    s = db_session
    when = datetime.now(UTC) + timedelta(hours=2)
    run = await create_link_run(
        s, project=SimpleNamespace(id=3), creator=SimpleNamespace(id=None),
        name="LINK HOP", task_type=RunTaskType.SITEWIDE_LINK.value,
        links=[{"url": "https://nawal.mx/", "anchor": "Nawal", "count": 4}],
        concurrency=2, timeout_seconds=40,
        proxy_selector="all", spread_days=2, scheduled_for=when,
    )
    fresh = await s.scalar(select(PostingRun).where(PostingRun.id == run.id))
    assert fresh.status == PostingRunStatus.SCHEDULED.value  # отложенный старт
    assert fresh.proxy_selector == "all"
    assert fresh.spread_days == 2
    # drip: у всех айтемов проставлен not_before в будущем (в окне старта)
    items = await _items(s, run.id)
    assert len(items) == 4
    assert all(it.not_before is not None for it in items)
    assert all(it.not_before >= when - timedelta(minutes=1) for it in items)
    assert all(it.not_before <= when + timedelta(days=2, minutes=1) for it in items)


async def test_create_link_run_defaults_ready_no_drip(db_session):
    """Без новых параметров: статус READY, прокси-селектор пуст, not_before NULL."""
    from infrastructure.db.models.posting import PostingRun, PostingRunStatus

    s = db_session
    run = await _mk_run(s, count=2)
    fresh = await s.scalar(select(PostingRun).where(PostingRun.id == run.id))
    assert fresh.status == PostingRunStatus.READY.value
    assert fresh.proxy_selector is None
    items = await _items(s, run.id)
    assert all(it.not_before is None for it in items)


async def test_hopping_passes_proxy_pool_to_place(db_session, monkeypatch):
    """process_link_item прокидывает proxy_urls в _place_on_site."""
    s = db_session
    run = await _mk_run(s, count=1)
    item = (await _items(s, run.id))[0]
    seen: dict = {}

    async def fake_candidates(session, *, exclude_site_ids=None, **kw):
        return [501]

    async def fake_place(item_id, site_id, task_type, url, anchor, proxy_urls=None):
        seen["proxy_urls"] = proxy_urls
        return {"ok": True, "status": "placed", "site_id": site_id}

    monkeypatch.setattr(wls, "candidate_link_sites", fake_candidates)
    monkeypatch.setattr(wls, "_place_on_site", fake_place)

    await wls.process_link_item(item.id, proxy_urls=["http://px:1", None])
    assert seen["proxy_urls"] == ["http://px:1", None]


async def test_hopping_skips_failed_then_places(db_session, monkeypatch):
    """Фейлы (login/place) → следующий сайт; на успехе пишем placed+site_id."""
    s = db_session
    run = await _mk_run(s, count=1)
    item = (await _items(s, run.id))[0]

    tried: list[int] = []

    async def fake_candidates(session, *, exclude_existing=True,
                              exclude_site_ids=None, **kw):
        ex = exclude_site_ids or set()
        return [x for x in (101, 102, 103) if x not in ex]

    async def fake_place(item_id, site_id, task_type, url, anchor, proxy_urls=None):
        tried.append(site_id)
        if site_id == 103:
            return {"ok": True, "status": "placed", "site_id": site_id, "domain": "s103"}
        return {"ok": False, "status": "login_auth_invalid", "domain": f"s{site_id}"}

    monkeypatch.setattr(wls, "candidate_link_sites", fake_candidates)
    monkeypatch.setattr(wls, "_place_on_site", fake_place)

    res = await wls.process_link_item(item.id)
    assert res["status"] == "placed" and res["site_id"] == 103
    assert tried == [101, 102, 103]  # перебрали по порядку, без повторов


async def test_hopping_exhausts_pool_marks_failed(db_session, monkeypatch):
    """Сайты кончились (все зафейлили) → айтем FAILED (бюджета нет — пул-терминатор)."""
    s = db_session
    run = await _mk_run(s, count=1)
    item = (await _items(s, run.id))[0]

    async def fake_candidates(session, *, exclude_existing=True,
                              exclude_site_ids=None, **kw):
        ex = exclude_site_ids or set()
        return [x for x in (201, 202) if x not in ex]

    async def fake_place(item_id, site_id, task_type, url, anchor, proxy_urls=None):
        return {"ok": False, "status": "login_auth_invalid", "domain": f"s{site_id}"}

    monkeypatch.setattr(wls, "candidate_link_sites", fake_candidates)
    monkeypatch.setattr(wls, "_place_on_site", fake_place)

    res = await wls.process_link_item(item.id)
    assert res["ok"] is False and res["status"] == "login_auth_invalid"
    status = await s.scalar(select(TextItem.status).where(TextItem.id == item.id))
    assert status == TextItemStatus.FAILED.value


async def test_hopping_skip_exists_short_circuits(db_session, monkeypatch):
    """skip_exists (сайт уже с нашей ссылкой) — терминальный, не валим айтем."""
    s = db_session
    run = await _mk_run(s, count=1)
    item = (await _items(s, run.id))[0]

    async def fake_candidates(session, *, exclude_site_ids=None, **kw):
        ex = exclude_site_ids or set()
        return [x for x in (301,) if x not in ex]

    async def fake_place(item_id, site_id, task_type, url, anchor, proxy_urls=None):
        return {"ok": True, "status": "skip_exists", "site_id": site_id}

    monkeypatch.setattr(wls, "candidate_link_sites", fake_candidates)
    monkeypatch.setattr(wls, "_place_on_site", fake_place)

    res = await wls.process_link_item(item.id)
    assert res["status"] == "skip_exists"


async def test_hopping_shared_registry_no_collision(db_session, monkeypatch):
    """Общий used_sites → параллельные айтемы не целят в один сайт."""
    s = db_session
    run = await _mk_run(s, count=2)
    items = await _items(s, run.id)
    assert len(items) == 2

    async def fake_candidates(session, *, exclude_site_ids=None, **kw):
        ex = exclude_site_ids or set()
        return [x for x in (401, 402) if x not in ex]

    async def fake_place(item_id, site_id, task_type, url, anchor, proxy_urls=None):
        await asyncio.sleep(0)  # уступаем луп — проверяем атомарность claim
        return {"ok": True, "status": "placed", "site_id": site_id, "domain": f"s{site_id}"}

    monkeypatch.setattr(wls, "candidate_link_sites", fake_candidates)
    monkeypatch.setattr(wls, "_place_on_site", fake_place)

    used: set[int] = set()
    res = await asyncio.gather(
        *[wls.process_link_item(it.id, used_sites=used) for it in items])
    assert sorted(r["site_id"] for r in res) == [401, 402]  # разные сайты
