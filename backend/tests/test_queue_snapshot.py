"""Global Queue = только активная нагрузка (running/queued/unpacking).
Спящие scheduled (drip между порциями), paused и waiting-for-user
(need_more_admins/needs_review) в очередь НЕ попадают — иначе фоновые
drip-раны и приостановленные забивают вывод и нельзя оценить загрузку."""
from __future__ import annotations

from domain.queue.service import get_queue_snapshot
from infrastructure.db.models import PostingRun, Project
from infrastructure.db.models.posting import PostingRunStatus as S
from sqlalchemy import text

_SHOWN = [S.RUNNING, S.QUEUED, S.UNPACKING]
_HIDDEN = [S.SCHEDULED, S.PAUSED, S.NEED_MORE_ADMINS, S.NEEDS_REVIEW]


async def test_queue_shows_only_active_load(db_session):
    s = db_session
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="QUEUE TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()

    ids: dict[str, int] = {}
    for st in _SHOWN + _HIDDEN:
        run = PostingRun(project_id=proj.id, name=f"Q {st.value}", status=st.value)
        s.add(run)
        await s.flush()
        ids[st.value] = run.id
    await s.commit()

    try:
        snap = await get_queue_snapshot(s)
        shown_ids = {p["id"] for p in snap["posting"]}

        for st in _SHOWN:
            assert ids[st.value] in shown_ids, f"{st.value} должен быть в очереди"
        for st in _HIDDEN:
            assert ids[st.value] not in shown_ids, f"{st.value} НЕ должен быть в очереди"

        # summary.posting_active не считает спящие/приостановленные
        mine_active = {ids[st.value] for st in _SHOWN}
        assert mine_active <= shown_ids
        # все показанные статусы — только из активного набора
        active_vals = {st.value for st in _SHOWN}
        assert all(p["status"] in active_vals for p in snap["posting"])
    finally:
        await s.execute(text("DELETE FROM posting_runs WHERE project_id=:p"),
                        {"p": proj.id})
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()


async def test_generation_shows_with_progress(db_session):
    """csv_campaign в процессе AI-генерации: статус unpacking (попадает в
    очередь как нагрузка) + прогресс генерации отдаётся (gen_done/gen_total →
    красный бар в UI)."""
    s = db_session
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="QUEUE TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.flush()
    run = PostingRun(
        project_id=proj.id, name="Q gen", status=S.UNPACKING.value,
        content_source="csv_campaign", content_mode="gen_per_post",
        gen_params={"gen_done": 45, "gen_total": 900},
    )
    s.add(run)
    await s.commit()
    try:
        snap = await get_queue_snapshot(s)
        item = next((p for p in snap["posting"] if p["id"] == run.id), None)
        assert item is not None, "генерящийся ран должен быть в очереди (нагрузка)"
        assert item["gen_done"] == 45 and item["gen_total"] == 900
    finally:
        await s.execute(text("DELETE FROM posting_runs WHERE project_id=:p"),
                        {"p": proj.id})
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()
