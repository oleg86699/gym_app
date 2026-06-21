"""Кеш working XML-RPC URL для wp_accesses.

Revision ID: 0007_wp_working_url
Revises: 0006_wp_partial_uniq
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_wp_working_url"
down_revision: str | Sequence[str] | None = "0006_wp_partial_uniq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wp_accesses", sa.Column("last_working_url", sa.String(500), nullable=True))
    op.add_column("wp_accesses", sa.Column("last_working_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("wp_accesses", "last_working_at")
    op.drop_column("wp_accesses", "last_working_url")
