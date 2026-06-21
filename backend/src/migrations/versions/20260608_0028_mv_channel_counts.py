"""wp_pool_summary_mv — добавить разбивку valid по каналам (rpc/admin).

Карточка «Channels» на /wp-sites показывает сколько рабочих cred подтверждено
через XML-RPC (Tier 1) vs admin login (Tier 2). Чтобы idle-путь (чтение из MV)
тоже отдавал эти цифры, пересоздаём MV с двумя новыми колонками.

Revision ID: 0028_mv_channel_counts
Revises: 0027_partition_text_items
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0028_mv_channel_counts"
down_revision = "0027_partition_text_items"
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
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'valid'
     AND last_validation_kind IN ('ok','manual_valid')) AS credentials_valid_rpc,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'valid'
     AND (last_validation_kind IS NULL OR last_validation_kind NOT IN ('ok','manual_valid'))) AS credentials_valid_admin,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'invalid') AS credentials_invalid,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'pending') AS credentials_pending,
  (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status = 'transient') AS credentials_transient,
  now() AS computed_at
"""


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS wp_pool_summary_mv")
    op.execute(_MV_SQL)
    op.execute("CREATE UNIQUE INDEX ix_wp_pool_summary_mv_id ON wp_pool_summary_mv (id)")


def downgrade() -> None:
    # вернуть версию без channel-колонок (как в 0026)
    op.execute("DROP MATERIALIZED VIEW IF EXISTS wp_pool_summary_mv")
    op.execute("""
        CREATE MATERIALIZED VIEW wp_pool_summary_mv AS
        SELECT 1::int AS id,
          (SELECT count(*) FROM wp_sites WHERE deleted_at IS NULL) AS sites_total,
          (SELECT count(*) FROM wp_sites WHERE deleted_at IS NULL AND is_active) AS sites_active,
          (SELECT count(*) FROM wp_sites s WHERE s.deleted_at IS NULL AND s.is_active
             AND EXISTS (SELECT 1 FROM wp_credentials c WHERE c.site_id=s.id AND c.deleted_at IS NULL AND c.cred_status='valid')) AS sites_usable,
          (SELECT count(*) FROM wp_sites s WHERE s.deleted_at IS NULL
             AND (NOT s.is_active OR NOT EXISTS (SELECT 1 FROM wp_credentials c WHERE c.site_id=s.id AND c.deleted_at IS NULL AND c.cred_status='valid'))) AS sites_unusable,
          (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL) AS credentials_total,
          (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status='valid') AS credentials_valid,
          (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status='invalid') AS credentials_invalid,
          (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status='pending') AS credentials_pending,
          (SELECT count(*) FROM wp_credentials WHERE deleted_at IS NULL AND cred_status='transient') AS credentials_transient,
          now() AS computed_at
    """)
    op.execute("CREATE UNIQUE INDEX ix_wp_pool_summary_mv_id ON wp_pool_summary_mv (id)")
