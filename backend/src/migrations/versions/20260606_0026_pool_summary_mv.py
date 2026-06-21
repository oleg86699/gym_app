"""wp_pool_summary_mv — materialized view для дешёвых summary-карточек.

ПРОБЛЕМА: карточки /wp-sites (sites_total/usable/unusable, cred valid/...)
полируются каждые 4 сек и делают ~8 COUNT-ов с JOIN-ами по wp_credentials.
На 600k сайтов + 1.8M cred это full/index scan на каждый polling → DB под
нагрузкой 4500+ запросов/час только на summary.

РЕШЕНИЕ: одно-строчный MV, refresh раз в минуту (cron) + сразу после batch
validation. Endpoint читает MV (1 строка, индекс по id) → <1ms. Когда идёт
активная валидация, фронт запрашивает live-режим для свежести.

Все cred-категории берутся из generated column `cred_status` (миграция
0025) — единый источник истины, рассинхрон невозможен.

REFRESH ... CONCURRENTLY требует UNIQUE index → добавляем на константный id.

Revision ID: 0026_pool_summary_mv
Revises: 0025_cred_status_generated
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0026_pool_summary_mv"
down_revision = "0025_cred_status_generated"
branch_labels = None
depends_on = None


_MV_SQL = """
CREATE MATERIALIZED VIEW wp_pool_summary_mv AS
SELECT
  1::int AS id,
  (SELECT count(*) FROM wp_sites WHERE deleted_at IS NULL) AS sites_total,
  (SELECT count(*) FROM wp_sites WHERE deleted_at IS NULL AND is_active) AS sites_active,
  (SELECT count(*) FROM wp_sites s
     WHERE s.deleted_at IS NULL AND s.is_active
       AND EXISTS (SELECT 1 FROM wp_credentials c
                   WHERE c.site_id = s.id AND c.deleted_at IS NULL
                     AND c.cred_status = 'valid')) AS sites_usable,
  (SELECT count(*) FROM wp_sites s
     WHERE s.deleted_at IS NULL
       AND (NOT s.is_active
            OR NOT EXISTS (SELECT 1 FROM wp_credentials c
                           WHERE c.site_id = s.id AND c.deleted_at IS NULL
                             AND c.cred_status = 'valid'))) AS sites_unusable,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL) AS credentials_total,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'valid') AS credentials_valid,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'invalid') AS credentials_invalid,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'pending') AS credentials_pending,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'transient') AS credentials_transient,
  now() AS computed_at
"""


def upgrade() -> None:
    op.execute(_MV_SQL)
    op.execute("CREATE UNIQUE INDEX ix_wp_pool_summary_mv_id ON wp_pool_summary_mv (id)")


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS wp_pool_summary_mv")
