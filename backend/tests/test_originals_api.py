"""GET /postings/{id}/originals — отдаёт оригиналы для ревью (gen_per_row/spin).

Регрессия: эндпоинт был объявлен с response_model=list["SpinOriginalRow"]
(forward-ref строкой) → Pydantic не мог разрезолвить тип при сериализации →
500 на КАЖДЫЙ запрос. Фронт молча гасил ошибку → блок ревью оригиналов никогда
не показывался. Ни один тест не дёргал эндпоинт по HTTP — поэтому и не поймали.
"""

from __future__ import annotations

from httpx import AsyncClient
from infrastructure.db.models import PostingRun, PostingRunStatus, Text
from sqlalchemy import text as sql


async def test_originals_endpoint_200_and_enriched(
    client: AsyncClient, super_admin_token: str, db_session
):
    s = db_session
    t1 = Text(body="<p>play at {casino|slots}</p>", title="T1", lang="en",
              source="generated", content_hash="orig-api-h1")
    t2 = Text(body="<p>{win|earn} big</p>", title="T2", lang="en",
              source="generated", content_hash="orig-api-h2")
    s.add_all([t1, t2])
    await s.flush()
    run = PostingRun(
        project_id=3, name="ORIG API POC", status=PostingRunStatus.READY.value,
        task_type="post", content_source="csv_campaign", content_mode="gen_per_row",
        priority="normal", total_texts=4,
        gen_params={
            "main_text_ids": [t1.id, t2.id],
            "fanout_groups": [
                {"text_id": t1.id, "link": "https://foo.test/", "anchor": "buy now", "count": 3},
                {"text_id": t2.id, "link": "https://bar.test/", "anchor": "click", "count": 1},
            ],
        },
    )
    s.add(run)
    await s.flush()
    rid, i1, i2 = run.id, t1.id, t2.id
    await s.commit()

    try:
        r = await client.get(f"/admin/api/postings/{rid}/originals")
        assert r.status_code == 200, r.text   # ← регрессия: было 500
        data = r.json()
        assert len(data) == 2
        by = {d["id"]: d for d in data}
        assert by[i1]["link"] == "https://foo.test/"
        assert by[i1]["anchor"] == "buy now"
        assert by[i1]["placements"] == 3
        assert by[i2]["placements"] == 1
        assert by[i1]["spintax"]      # тело-спинтакс отдаётся для ревью
        assert by[i1]["title"] == "T1"
    finally:
        await s.execute(sql("DELETE FROM posting_runs WHERE id=:r"), {"r": rid})
        await s.execute(sql("DELETE FROM texts WHERE id = ANY(:i)"), {"i": [i1, i2]})
        await s.commit()
