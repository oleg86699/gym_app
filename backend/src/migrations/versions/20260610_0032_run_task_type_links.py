"""Phase 1 (link types): posting_runs.task_type + text_items link/placement поля.

  posting_runs.task_type — 'post' (default) | 'sitewide_link' | 'homepage_link'
  text_items:
    link_url, link_anchor        — что ставим (для link-типов; для post NULL)
    placed_via, placement_ref    — как/где разместили (verify, идемпотентность, удаление)
    verified_at, verified_urls   — подтверждение наличия ссылки (анонимно)

text_items партиционирована (0027) — ADD COLUMN на родителе пропагируется на партиции.

Revision ID: 0032_run_task_type_links
Revises: 0031_cred_provisioned
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_run_task_type_links"
down_revision = "0031_cred_provisioned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column("task_type", sa.String(length=20), nullable=False,
                  server_default="post"),
    )
    op.add_column("text_items", sa.Column("link_url", sa.Text(), nullable=True))
    op.add_column("text_items", sa.Column("link_anchor", sa.String(length=500), nullable=True))
    op.add_column("text_items", sa.Column("placed_via", sa.String(length=20), nullable=True))
    op.add_column("text_items", sa.Column("placement_ref", sa.String(length=255), nullable=True))
    op.add_column("text_items", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("text_items", sa.Column("verified_urls", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("text_items", "verified_urls")
    op.drop_column("text_items", "verified_at")
    op.drop_column("text_items", "placement_ref")
    op.drop_column("text_items", "placed_via")
    op.drop_column("text_items", "link_anchor")
    op.drop_column("text_items", "link_url")
    op.drop_column("posting_runs", "task_type")
