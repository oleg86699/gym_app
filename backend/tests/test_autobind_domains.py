"""Авто-привязка целевых доменов задачи к проекту + авто-резолв needs_review.

«Забыл добавить домен» safety-net: домены явных ссылок задачи (кампания/link/
csv-direct) привязываются к проекту, и уже загруженные needs_review-txt с этим
доменом среди кандидатов авто-резолвятся в pending.
"""

from __future__ import annotations

import core.celery_app as cel
from domain.project_domains import autobind_link_domains
from infrastructure.db.models import (
    PostingRun,
    PostingRunStatus,
    TextItem,
    TextItemStatus,
)
from sqlalchemy import text as sql


async def test_autobind_binds_and_resolves_needs_review(db_session, monkeypatch):
    s = db_session
    # redispatch не должен стучаться в реальный Celery
    monkeypatch.setattr(cel.celery_app, "send_task", lambda *a, **k: None)

    DOM = "autobind-xyztest.com"
    cand = [{"link": f"https://{DOM}/page", "anchor": "play now",
             "domain": DOM, "is_project_domain": False}]

    run = PostingRun(
        project_id=3, name="AUTOBIND POC",
        status=PostingRunStatus.NEEDS_REVIEW.value, task_type="post",
        content_source="upload_txt", priority="normal", total_texts=1,
    )
    s.add(run)
    await s.flush()
    item = TextItem(
        posting_run_id=run.id, project_id=3, original_filename="f.txt",
        content_hash="autobind-poc-hash", byte_size=10,
        status=TextItemStatus.NEEDS_REVIEW.value, link_candidates=cand,
    )
    s.add(item)
    await s.flush()
    rid, iid = run.id, item.id
    await s.commit()

    try:
        # домена ещё нет в проекте → needs_review
        added = await autobind_link_domains(s, 3, [f"https://{DOM}/whatever", f"https://{DOM}/x"])
        assert added == 1  # один уникальный домен добавлен (дедуп URL-ов)

        # домен привязан к проекту
        bound = await s.scalar(sql(
            "SELECT count(*) FROM project_domains WHERE project_id=3 AND domain=:d"), {"d": DOM})
        assert bound == 1

        # needs_review-задача авто-резолвлена: pending + target проставлен
        row = (await s.execute(sql(
            "SELECT status, target_domain, link_url FROM text_items WHERE id=:i"), {"i": iid})).first()
        assert row[0] == "pending"
        assert row[1] == DOM
        assert row[2] == f"https://{DOM}/page"

        # повторный вызов идемпотентен — домен уже есть, ничего не добавляет
        assert await autobind_link_domains(s, 3, [f"https://{DOM}/again"]) == 0
    finally:
        await s.execute(sql("DELETE FROM text_items WHERE id=:i"), {"i": iid})
        await s.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": rid})
        await s.execute(sql("DELETE FROM project_domains WHERE project_id=3 AND domain=:d"), {"d": DOM})
        await s.commit()
