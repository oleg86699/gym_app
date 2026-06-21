"""B1: единая библиотека текстов (тела в БД) + text_items.text_id.

Тела текстов переезжают из MinIO в Postgres (таблица texts): дедуп по
content_hash, язык, body_tsv (GIN, под будущий FTS-поиск), lifecycle.
text_items ссылается на texts через text_id. MinIO остаётся fallback на
переходный период (storage_key не трогаем).

Revision ID: 0036_texts_library
Revises: 0035_project_domains_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_texts_library"
down_revision = "0035_project_domains_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "texts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("lang", sa.String(length=10), nullable=True),
        # human | generated | reused
        sa.Column("source", sa.String(length=20), nullable=False, server_default="human"),
        sa.Column("gen_model", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    # body_tsv — генерируемая STORED колонка (язык-агностичный 'simple'), под FTS.
    op.execute(
        "ALTER TABLE texts ADD COLUMN body_tsv tsvector "
        "GENERATED ALWAYS AS "
        "(to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(body,''))) STORED"
    )
    op.create_index("ix_texts_body_tsv", "texts", ["body_tsv"], postgresql_using="gin")
    op.create_index("ix_texts_content_hash", "texts", ["content_hash"])

    op.add_column("text_items", sa.Column("text_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_text_items_text", "text_items", "texts",
        ["text_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_text_items_text_id", "text_items", ["text_id"])


def downgrade() -> None:
    op.drop_index("ix_text_items_text_id", table_name="text_items")
    op.drop_constraint("fk_text_items_text", "text_items", type_="foreignkey")
    op.drop_column("text_items", "text_id")
    op.drop_index("ix_texts_content_hash", table_name="texts")
    op.drop_index("ix_texts_body_tsv", table_name="texts")
    op.drop_table("texts")
