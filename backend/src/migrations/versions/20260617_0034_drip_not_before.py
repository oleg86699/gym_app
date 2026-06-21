"""drip-feed: text_items.not_before + posting_runs.spread_days.

`not_before` — задачу не брать в работу раньше этого момента (per-task
отложенный старт). `spread_days` на run — на сколько дней «размазать» все
text_items (link velocity). Окно стартует от scheduled_for (или now).

Воркер постит только due-задачи (not_before <= now), а если остались будущие —
перевзводит run в status='scheduled' на момент ближайшей порции (cron
dispatch_scheduled_runs поднимет). Run «засыпает» между порциями, не держит
worker-слот сутками.

Revision ID: 0034_drip_not_before
Revises: 0033_global_posting_concurrency
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_drip_not_before"
down_revision = "0033_global_posting_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "text_items",
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "posting_runs",
        sa.Column("spread_days", sa.Integer(), nullable=False, server_default="0"),
    )
    # Индекс под выборку due-задач (partitioned parent → пропагируется на партиции).
    op.create_index(
        "ix_text_items_run_pending_due",
        "text_items",
        ["posting_run_id", "status", "not_before"],
    )


def downgrade() -> None:
    op.drop_index("ix_text_items_run_pending_due", table_name="text_items")
    op.drop_column("posting_runs", "spread_days")
    op.drop_column("text_items", "not_before")
