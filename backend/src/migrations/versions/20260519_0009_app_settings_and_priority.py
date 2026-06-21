"""AppSettings (singleton) + posting_runs.priority.

Revision ID: 0009_app_settings_prio
Revises: 0008_wp_sites_creds
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_app_settings_prio"
down_revision: str | Sequence[str] | None = "0008_wp_sites_creds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── app_settings (singleton) ──────────────────────────────────────
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "default_concurrency",
            sa.Integer(),
            nullable=False,
            server_default="25",
        ),
        sa.Column(
            "default_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Гарантия что строка ровно одна: PK=1, плюс CHECK
    op.create_check_constraint("ck_app_settings_singleton", "app_settings", "id = 1")
    op.execute(
        "INSERT INTO app_settings (id, default_concurrency, default_timeout_seconds) "
        "VALUES (1, 25, 30)"
    )

    # ── posting_runs.priority ─────────────────────────────────────────
    op.add_column(
        "posting_runs",
        sa.Column(
            "priority",
            sa.String(16),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_index(
        "ix_posting_runs_priority_status", "posting_runs", ["priority", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_posting_runs_priority_status", table_name="posting_runs")
    op.drop_column("posting_runs", "priority")
    op.drop_table("app_settings")
