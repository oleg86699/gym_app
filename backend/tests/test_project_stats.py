"""available_admins / valid_admins_pool = пул ПОСТАБЕЛЬНЫХ САЙТОВ (ровно тот,
что воркер берёт в _pick_candidate_sites), а НЕ сырой счётчик credentials с
is_valid=TRUE (он множил по нескольку cred на сайт и ловил transient/pending —
давал завышенное число, не совпадающее с реальным пулом постинга)."""
from __future__ import annotations

from domain.projects.service import compute_project_stats
from infrastructure.db.models import Project
from sqlalchemy import text

# Предикат = _pick_candidate_sites: активный сайт + ≥1 cred cred_status='valid'
# с подтверждённым каналом постинга (xmlrpc|admin).
_POSTABLE_SITES = text("""
    SELECT count(*) FROM wp_sites s
    WHERE s.deleted_at IS NULL AND s.is_active IS TRUE
      AND EXISTS (
        SELECT 1 FROM wp_credentials c
        WHERE c.site_id = s.id AND c.deleted_at IS NULL
          AND c.cred_status = 'valid'
          AND (c.can_post_via_xmlrpc IS TRUE OR c.can_post_via_admin IS TRUE))
""")

# Старая (ошибочная) формула — для контраста: creds is_valid=TRUE на активных
# сайтах. Почти всегда > пула постабельных сайтов (множит cred + ловит transient).
_OLD_ISVALID_CREDS = text("""
    SELECT count(*) FROM wp_credentials c JOIN wp_sites s ON s.id = c.site_id
    WHERE c.deleted_at IS NULL AND c.is_valid IS TRUE
      AND s.deleted_at IS NULL AND s.is_active IS TRUE
""")


async def test_available_admins_matches_postable_site_pool(db_session):
    s = db_session
    expected_pool = int((await s.execute(_POSTABLE_SITES)).scalar_one())
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="STATS TEST", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.commit()
    try:
        st = (await compute_project_stats(s, [proj.id]))[proj.id]
        # пул = постабельные сайты (как у воркера)
        assert st["valid_admins_pool"] == expected_pool
        # свежий проект (0 использований) → доступно = весь пул
        assert st["available_admins"] == expected_pool
    finally:
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()


async def test_pool_is_not_raw_isvalid_cred_count(db_session):
    """Регресс-страховка: пул считается по сайтам, не по credentials is_valid.
    Если в dev-БД есть сайты с несколькими cred / transient — числа разойдутся,
    и тест поймает откат к старой формуле."""
    s = db_session
    postable = int((await s.execute(_POSTABLE_SITES)).scalar_one())
    old_creds = int((await s.execute(_OLD_ISVALID_CREDS)).scalar_one())
    owner = await s.scalar(text("SELECT id FROM admin_users ORDER BY id LIMIT 1"))
    proj = Project(name="STATS TEST2", is_active=True, owner_user_id=owner)
    s.add(proj)
    await s.commit()
    try:
        pool = (await compute_project_stats(s, [proj.id]))[proj.id]["valid_admins_pool"]
        assert pool == postable
        # на реальной dev-БД старая формула завышена → не должны совпасть
        if old_creds != postable:
            assert pool != old_creds
    finally:
        await s.execute(text("DELETE FROM projects WHERE id=:p"), {"p": proj.id})
        await s.commit()
