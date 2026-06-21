"""app_settings: global_posting_concurrency (потолок параллельных постов).

Жёсткий лимитер дефицитного ресурса: суммарное число одновременных постов
через ВСЕ posting-run-ы и оба celery-процесса. Делится между активными
run-ами → «всё двигается понемногу» без приоритета и governor-а.

Revision ID: 0033_global_posting_concurrency
Revises: 0032_run_task_type_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_global_posting_concurrency"
down_revision = "0032_run_task_type_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "global_posting_concurrency",
            sa.Integer(),
            nullable=False,
            server_default="80",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "global_posting_concurrency")
