"""mv sites_postable: сайты с ПОДТВЕРЖДЁННЫМ каналом постинга (не только валидным логином).

USABLE в MV = active + ≥1 valid cred (логин удался). Но постить можно только там,
где validation подтвердила КАНАЛ (can_post_via_xmlrpc OR can_post_via_admin) — это
и есть реальный пул постинга (_pick_candidate_sites). Пользователи путали «логин ок»
с «постинг ок», поэтому добавляем отдельный счётчик sites_postable, чтобы показать
его в карточке /wp-sites. Пересоздаём MV (в MV нельзя ALTER-добавить колонку).

Revision ID: 0062_mv_sites_postable
Revises: 0061_supplier_link_enc
"""
from alembic import op

revision = "0062_mv_sites_postable"
down_revision = "0061_supplier_link_enc"
branch_labels = None
depends_on = None

_MV_NEW = """
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
     WHERE s.deleted_at IS NULL AND s.is_active
       AND EXISTS (SELECT 1 FROM wp_credentials c
                   WHERE c.site_id = s.id AND c.deleted_at IS NULL
                     AND c.cred_status = 'valid'
                     AND (c.can_post_via_xmlrpc IS TRUE
                          OR c.can_post_via_admin IS TRUE))) AS sites_postable,
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

_MV_OLD = """
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
    op.execute(_MV_NEW)
    op.execute("CREATE UNIQUE INDEX ix_wp_pool_summary_mv_id ON wp_pool_summary_mv (id)")


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS wp_pool_summary_mv")
    op.execute(_MV_OLD)
    op.execute("CREATE UNIQUE INDEX ix_wp_pool_summary_mv_id ON wp_pool_summary_mv (id)")
