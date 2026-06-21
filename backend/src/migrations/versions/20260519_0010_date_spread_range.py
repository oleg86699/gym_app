"""Date spread → диапазон min..max + переезд в app_settings.

- AppSettings: + default_spread_min_days, + default_spread_max_days
- PostingRun: period_days → spread_min_days (0) + spread_max_days (was period_days)

Revision ID: 0010_date_spread_range
Revises: 0009_app_settings_prio
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_date_spread_range"
down_revision: str | Sequence[str] | None = "0009_app_settings_prio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── app_settings: новые дефолты окна публикации ────────────────────
    op.add_column(
        "app_settings",
        sa.Column(
            "default_spread_min_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "app_settings",
        sa.Column(
            "default_spread_max_days",
            sa.Integer(),
            nullable=False,
            server_default="45",
        ),
    )

    # ── posting_runs: period_days → spread_min/max_days ────────────────
    op.add_column(
        "posting_runs",
        sa.Column(
            "spread_min_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "posting_runs",
        sa.Column(
            "spread_max_days",
            sa.Integer(),
            nullable=False,
            server_default="45",
        ),
    )
    # Backfill spread_max_days = period_days для уже созданных прогонов
    op.execute("UPDATE posting_runs SET spread_max_days = period_days")
    op.drop_column("posting_runs", "period_days")


def downgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column(
            "period_days",
            sa.Integer(),
            nullable=False,
            server_default="45",
        ),
    )
    op.execute("UPDATE posting_runs SET period_days = spread_max_days")
    op.drop_column("posting_runs", "spread_max_days")
    op.drop_column("posting_runs", "spread_min_days")
    op.drop_column("app_settings", "default_spread_max_days")
    op.drop_column("app_settings", "default_spread_min_days")
