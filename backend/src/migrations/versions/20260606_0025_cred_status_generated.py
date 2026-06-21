"""cred_status — generated column как единый источник истины статуса cred.

ПРОБЛЕМА: логика «valid/invalid/transient/pending» дублировалась в ~5 местах
(pool_summary SQL, batch counter SQL, _cred_category Python, list filters,
UI badges). Любое расхождение → рассинхрон цифр между страницами (что мы
ловили многократно).

РЕШЕНИЕ: STORED generated column на самой строке cred. Один CASE-expression
в БД определяет статус. Все агрегаты/фильтры/MV читают его — рассинхрон
физически невозможен.

Приоритет категорий (как было): invalid > pending > valid > transient.
  invalid   — is_valid=false ИЛИ kind ∈ (auth_invalid/permission_denied/manual_invalid)
  pending   — ещё не валидировали (last_validated_at IS NULL)
  valid     — Tier1 ok/manual_valid ИЛИ legacy(kind=NULL) ИЛИ Tier2 admin login
  transient — провалидировали, но ни один канал не подтвердил (inconclusive)

Generated column зависит только от колонок той же строки (is_valid,
last_validation_kind, last_validated_at, can_admin_login) — STORED работает.

Revision ID: 0025_cred_status_generated
Revises: 0024_proxy_health
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0025_cred_status_generated"
down_revision = "0024_proxy_health"
branch_labels = None
depends_on = None


_CRED_STATUS_EXPR = """
CASE
  WHEN is_valid IS FALSE
       OR last_validation_kind IN ('auth_invalid','permission_denied','manual_invalid')
    THEN 'invalid'
  WHEN last_validated_at IS NULL
    THEN 'pending'
  WHEN last_validation_kind IN ('ok','manual_valid')
       OR last_validation_kind IS NULL
       OR can_admin_login IS TRUE
    THEN 'valid'
  ELSE 'transient'
END
"""


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE wp_credentials "
        f"ADD COLUMN cred_status text "
        f"GENERATED ALWAYS AS ({_CRED_STATUS_EXPR}) STORED"
    )
    # Индекс для быстрых GROUP BY / WHERE cred_status (учитываем soft-delete).
    op.execute(
        "CREATE INDEX ix_wp_credentials_status "
        "ON wp_credentials (cred_status) WHERE deleted_at IS NULL"
    )
    # Композитный для per-site агрегатов (usable/unusable).
    op.execute(
        "CREATE INDEX ix_wp_credentials_site_status "
        "ON wp_credentials (site_id, cred_status) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_wp_credentials_site_status")
    op.execute("DROP INDEX IF EXISTS ix_wp_credentials_status")
    op.execute("ALTER TABLE wp_credentials DROP COLUMN IF EXISTS cred_status")
