"""WpCredential provisioning-поля: помечаем креды, которые мы СОЗДАЛИ сами
(provision-author) на сайтах, где мы администратор.

  provisioned             — флаг «создан нами»
  provisioned_at          — когда создали
  provisioned_by_cred_id  — каким admin-кредом создавали (self-FK, SET NULL)
  provisioned_via         — 'form' (user-new.php) | 'rest' (/wp/v2/users)

Частичный индекс по provisioned=true — быстро находить сайты, где наш cred
уже есть (для идемпотентного bulk «создать там, где ещё нет»).

Revision ID: 0031_cred_provisioned
Revises: 0030_cred_can_create_users
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_cred_provisioned"
down_revision = "0030_cred_can_create_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wp_credentials",
        sa.Column("provisioned", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "wp_credentials",
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wp_credentials",
        sa.Column("provisioned_by_cred_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "wp_credentials",
        sa.Column("provisioned_via", sa.String(length=16), nullable=True),
    )
    op.create_foreign_key(
        "fk_wp_credentials_provisioned_by",
        "wp_credentials", "wp_credentials",
        ["provisioned_by_cred_id"], ["id"], ondelete="SET NULL",
    )
    # Частичный индекс: какие сайты УЖЕ имеют наш provisioned cred.
    op.create_index(
        "ix_wp_credentials_provisioned_site",
        "wp_credentials", ["site_id"],
        unique=False,
        postgresql_where=sa.text("provisioned IS TRUE AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_wp_credentials_provisioned_site", table_name="wp_credentials")
    op.drop_constraint(
        "fk_wp_credentials_provisioned_by", "wp_credentials", type_="foreignkey"
    )
    op.drop_column("wp_credentials", "provisioned_via")
    op.drop_column("wp_credentials", "provisioned_by_cred_id")
    op.drop_column("wp_credentials", "provisioned_at")
    op.drop_column("wp_credentials", "provisioned")
