"""C1: Content Engine каркас — поля для генерации/спина/reuse как режимов.

texts:        +spin_formula, +reusable, +used_as_original, +parent_text_id
posting_runs: +content_source, +content_mode, +run_mode, +gen_params
app_settings: +max_spin_reuse (дефолт 50)

Статусы text_items (awaiting_generation/awaiting_review) и run-статусы —
строковые значения в varchar, отдельной DDL не требуют.

Revision ID: 0038_content_engine
Revises: 0037_texts_search
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_content_engine"
down_revision = "0037_texts_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── texts: спин + reuse + происхождение ───────────────────────────
    op.add_column("texts", sa.Column("spin_formula", sa.Text(), nullable=True))
    op.add_column("texts", sa.Column("reusable", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    op.add_column("texts", sa.Column("used_as_original", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    op.add_column("texts", sa.Column("parent_text_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_texts_parent", "texts", "texts",
                          ["parent_text_id"], ["id"], ondelete="SET NULL")
    # частичный индекс под reuse-пикер (только переиспользуемые со спином)
    op.execute(
        "CREATE INDEX ix_texts_reuse_pool ON texts (lang, times_used) "
        "WHERE reusable AND spin_formula IS NOT NULL AND archived_at IS NULL"
    )

    # ── posting_runs: режимы источника контента ───────────────────────
    op.add_column("posting_runs", sa.Column("content_source", sa.String(length=20),
                  nullable=False, server_default="upload_txt"))
    op.add_column("posting_runs", sa.Column("content_mode", sa.String(length=20), nullable=True))
    op.add_column("posting_runs", sa.Column("run_mode", sa.String(length=10),
                  nullable=False, server_default="auto"))
    op.add_column("posting_runs", sa.Column("gen_params", postgresql.JSONB(), nullable=True))

    # ── app_settings: потолок reuse ───────────────────────────────────
    op.add_column("app_settings", sa.Column("max_spin_reuse", sa.Integer(),
                  nullable=False, server_default="50"))


def downgrade() -> None:
    op.drop_column("app_settings", "max_spin_reuse")
    op.drop_column("posting_runs", "gen_params")
    op.drop_column("posting_runs", "run_mode")
    op.drop_column("posting_runs", "content_mode")
    op.drop_column("posting_runs", "content_source")
    op.execute("DROP INDEX IF EXISTS ix_texts_reuse_pool")
    op.drop_constraint("fk_texts_parent", "texts", type_="foreignkey")
    op.drop_column("texts", "parent_text_id")
    op.drop_column("texts", "used_as_original")
    op.drop_column("texts", "reusable")
    op.drop_column("texts", "spin_formula")
