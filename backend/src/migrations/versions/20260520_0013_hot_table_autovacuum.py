"""Autovacuum-настройки для горячих таблиц + fillfactor для text_items.

Поднимаем autovacuum агрессивнее на posting_runs (хотрее всего из-за
непрерывных UPDATE счётчиков), text_items (HOT-updates статуса) и
project_wp_used (массовые INSERT-ы). См. план stage 2 §1 и ADR-011.

Revision ID: 0013_hot_autovacuum
Revises: 0012_project_reuse_limit
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_hot_autovacuum"
down_revision: str | Sequence[str] | None = "0012_project_reuse_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE posting_runs SET (
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_analyze_scale_factor = 0.01,
            autovacuum_vacuum_cost_delay = 10
        )
        """
    )
    op.execute(
        """
        ALTER TABLE text_items SET (
            autovacuum_vacuum_scale_factor = 0.05,
            autovacuum_analyze_scale_factor = 0.02,
            fillfactor = 80
        )
        """
    )
    op.execute(
        """
        ALTER TABLE project_wp_used SET (
            autovacuum_vacuum_scale_factor = 0.1
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE posting_runs RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, autovacuum_vacuum_cost_delay)")
    op.execute("ALTER TABLE text_items RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, fillfactor)")
    op.execute("ALTER TABLE project_wp_used RESET (autovacuum_vacuum_scale_factor)")
