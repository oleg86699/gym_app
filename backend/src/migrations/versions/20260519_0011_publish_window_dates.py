"""Окно публикации: spread_min/max_days → publish_from/publish_to (Date).

Revision ID: 0011_publish_window
Revises: 0010_date_spread_range
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_publish_window"
down_revision: str | Sequence[str] | None = "0010_date_spread_range"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # app_settings
    op.add_column("app_settings", sa.Column("default_publish_from", sa.Date(), nullable=True))
    op.add_column("app_settings", sa.Column("default_publish_to", sa.Date(), nullable=True))
    # Backfill: spread_max=0 → ничего, иначе [today, today+spread_max_days]
    op.execute(
        """
        UPDATE app_settings
           SET default_publish_from = CURRENT_DATE + (default_spread_min_days || ' days')::interval,
               default_publish_to   = CURRENT_DATE + (default_spread_max_days || ' days')::interval
         WHERE default_spread_max_days > 0
        """
    )
    op.drop_column("app_settings", "default_spread_min_days")
    op.drop_column("app_settings", "default_spread_max_days")

    # posting_runs
    op.add_column("posting_runs", sa.Column("publish_from", sa.Date(), nullable=True))
    op.add_column("posting_runs", sa.Column("publish_to", sa.Date(), nullable=True))
    op.execute(
        """
        UPDATE posting_runs
           SET publish_from = (created_at AT TIME ZONE 'UTC')::date + (spread_min_days || ' days')::interval,
               publish_to   = (created_at AT TIME ZONE 'UTC')::date + (spread_max_days || ' days')::interval
         WHERE spread_max_days > 0
        """
    )
    op.drop_column("posting_runs", "spread_min_days")
    op.drop_column("posting_runs", "spread_max_days")


def downgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column("spread_min_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "posting_runs",
        sa.Column("spread_max_days", sa.Integer(), nullable=False, server_default="45"),
    )
    op.drop_column("posting_runs", "publish_to")
    op.drop_column("posting_runs", "publish_from")

    op.add_column(
        "app_settings",
        sa.Column("default_spread_min_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "app_settings",
        sa.Column("default_spread_max_days", sa.Integer(), nullable=False, server_default="45"),
    )
    op.drop_column("app_settings", "default_publish_to")
    op.drop_column("app_settings", "default_publish_from")
