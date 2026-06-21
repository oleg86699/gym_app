"""Proxies table + FK posting_runs.proxy_id (он уже был nullable Integer
с миграции 0005, но без FK).

Revision ID: 0015_proxies
Revises: 0014_encrypt_wp_passwords
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_proxies"
down_revision: str | Sequence[str] | None = "0014_encrypt_wp_passwords"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proxies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("protocol", sa.String(10), nullable=False, server_default="http"),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.String(500), nullable=True),
        sa.Column("country", sa.String(10), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("proxy_type", sa.String(20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_error", sa.String(500), nullable=True),
        sa.Column("external_ip", sa.String(45), nullable=True),
        sa.Column("isp", sa.String(255), nullable=True),
        sa.Column("asn", sa.String(255), nullable=True),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Уникальный индекс на (source, source_id) когда оба заданы — для upsert-а
    # при ре-импорте из провайдера.
    op.create_index(
        "ux_proxies_source_source_id",
        "proxies",
        ["source", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index("ix_proxies_status_active", "proxies", ["is_active", "status"])
    op.create_index("ix_proxies_provider", "proxies", ["provider"])

    # Привязка posting_runs.proxy_id (колонка уже была, теперь FK + индекс)
    op.create_foreign_key(
        "fk_posting_runs_proxy",
        "posting_runs",
        "proxies",
        ["proxy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_posting_runs_proxy_id", "posting_runs", ["proxy_id"])


def downgrade() -> None:
    op.drop_index("ix_posting_runs_proxy_id", table_name="posting_runs")
    op.drop_constraint("fk_posting_runs_proxy", "posting_runs", type_="foreignkey")
    op.drop_index("ix_proxies_provider", table_name="proxies")
    op.drop_index("ix_proxies_status_active", table_name="proxies")
    op.drop_index("ux_proxies_source_source_id", table_name="proxies")
    op.drop_table("proxies")
