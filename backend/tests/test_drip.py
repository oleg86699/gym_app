"""Drip-feed: _pick_pending_batch берёт только «созревшие» задачи (not_before)."""

from __future__ import annotations

from sqlalchemy import text

from workers.celery.posting import _pick_pending_batch

_RUN_INS = (
    "INSERT INTO posting_runs "
    "(project_id,name,status,task_type,concurrency,timeout_seconds,total_texts,"
    " priority,posting_method,spread_days,created_at,updated_at) "
    "VALUES (3,'TEST drip','running','post',25,30,0,'normal','auto',0,now(),now()) "
    "RETURNING id"
)
_ITEM_INS = (
    "INSERT INTO text_items "
    "(posting_run_id,project_id,original_filename,content_hash,byte_size,status,"
    " created_at,not_before) "
    "VALUES (:r,3,:fn,:h,10,'pending',now(),{nb})"
)


async def test_pick_pending_respects_not_before(db_session):
    s = db_session
    rid = (await s.execute(text(_RUN_INS))).scalar_one()
    await s.commit()
    try:
        # due: not_before NULL
        await s.execute(text(_ITEM_INS.format(nb="NULL")),
                        {"r": rid, "fn": "due_null.txt", "h": "a" * 64})
        # due: в прошлом
        await s.execute(text(_ITEM_INS.format(nb="now() - interval '1 hour'")),
                        {"r": rid, "fn": "due_past.txt", "h": "b" * 64})
        # НЕ due: в будущем
        await s.execute(text(_ITEM_INS.format(nb="now() + interval '2 days'")),
                        {"r": rid, "fn": "future.txt", "h": "c" * 64})
        await s.commit()

        batch = await _pick_pending_batch(s, rid, 10)
        picked = {it.original_filename for it in batch}
        # взяты только две созревшие, будущая — нет
        assert picked == {"due_null.txt", "due_past.txt"}, picked

        # будущая задача осталась pending (её никто не забрал)
        still_pending = (await s.execute(text(
            "SELECT count(*) FROM text_items WHERE posting_run_id=:r "
            "AND status='pending' AND not_before > now()"), {"r": rid})).scalar_one()
        assert still_pending == 1

        # ближайшая будущая порция (для re-arm scheduled_for) существует
        next_due = (await s.execute(text(
            "SELECT min(not_before) FROM text_items WHERE posting_run_id=:r "
            "AND status='pending' AND not_before > now()"), {"r": rid})).scalar_one()
        assert next_due is not None
    finally:
        await s.execute(text("DELETE FROM posting_runs WHERE id=:r"), {"r": rid})
        await s.commit()
