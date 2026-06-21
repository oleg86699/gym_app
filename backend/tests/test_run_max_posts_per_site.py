"""Лимит повторного использования сайта переехал на ЗАДАЧУ (per-run).

Пиннит ключевое: `_pick_candidate_sites` фильтрует по
`posting_runs.max_posts_per_site` (НЕ по проекту) и читает значение live —
поднятие лимита задачи сразу возвращает «исчерпанный» сайт в кандидаты.
Это покрывает SQL, который юнит-моки в других тестах не выполняют.
"""
from __future__ import annotations

from infrastructure.db.models import PostingRun, Project
from sqlalchemy import text
from workers.celery.posting import _pick_candidate_sites


async def test_pick_candidate_honors_run_max_posts_per_site(db_session):
    s = db_session
    owner_id = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    # наименьший id среди реально постабельных сайтов (valid cred + канал постинга)
    site_id = await s.scalar(text("""
        SELECT min(c.site_id) FROM wp_credentials c
        JOIN wp_sites si ON si.id = c.site_id
        WHERE c.cred_status = 'valid' AND c.deleted_at IS NULL
          AND (c.can_post_via_xmlrpc IS TRUE OR c.can_post_via_admin IS TRUE)
          AND si.is_active IS TRUE AND si.deleted_at IS NULL
    """))
    assert site_id is not None, "в dev-БД нет постабельных сайтов — тест не применим"

    # свежий проект (0 записей в project_wp_used) + задача с лимитом 1
    proj = Project(name="MPPS TEST", is_active=True, owner_user_id=owner_id)
    s.add(proj)
    await s.flush()
    run = PostingRun(project_id=proj.id, name="MPPS RUN", status="draft",
                     max_posts_per_site=1)
    s.add(run)
    await s.commit()

    def ids(rows):
        return {x.id for x in rows}

    try:
        # 0 использований < 1 → сайт в кандидатах
        r = await _pick_candidate_sites(
            s, project_id=proj.id, run_id=run.id, exclude_site_ids=set(), limit=80)
        assert site_id in ids(r)

        # 1 использование >= лимит 1 → исключён
        await s.execute(text(
            "INSERT INTO project_wp_used(project_id, posting_run_id, site_id, created_at) "
            "VALUES (:p, :r, :si, now())"), {"p": proj.id, "r": run.id, "si": site_id})
        await s.commit()
        r = await _pick_candidate_sites(
            s, project_id=proj.id, run_id=run.id, exclude_site_ids=set(), limit=80)
        assert site_id not in ids(r)

        # поднимаем лимит ЗАДАЧИ до 2 → 1 < 2 → снова кандидат (live run-level read).
        # Если бы фильтр читал проект — этот шаг бы не сработал.
        run.max_posts_per_site = 2
        await s.commit()
        r = await _pick_candidate_sites(
            s, project_id=proj.id, run_id=run.id, exclude_site_ids=set(), limit=80)
        assert site_id in ids(r)
    finally:
        await s.execute(text("DELETE FROM project_wp_used WHERE project_id=:p"),
                        {"p": proj.id})
        await s.execute(text("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()


async def test_create_run_persists_max_posts_per_site(db_session):
    """create_run сохраняет per-run лимит (дефолт 1, либо явный)."""
    from domain.postings.service import create_run

    s = db_session
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="MPPS TEST2", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()

    class _U:
        id = owner

    run = await create_run(
        s, project=proj, creator=_U(), name="R", publish_from=None, publish_to=None,
        concurrency=5, timeout_seconds=40, priority="normal", scheduled_for=None,
        source_archive_storage_key="k", max_posts_per_site=4)
    try:
        assert run.max_posts_per_site == 4
    finally:
        await s.execute(text("DELETE FROM posting_runs WHERE id=:r"), {"r": run.id})
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()
