"""B2-search: pg_trgm для нечёткого поиска по texts.title.

FTS уже обеспечен body_tsv (GIN, миграция 0036). Здесь добавляем pg_trgm
(нечёткое совпадение коротких полей — title/анкор) + GIN-trgm индекс на title.

Revision ID: 0037_texts_search
Revises: 0036_texts_library
"""
from __future__ import annotations

from alembic import op

revision = "0037_texts_search"
down_revision = "0036_texts_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_texts_title_trgm "
        "ON texts USING gin (title gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_texts_title_trgm")
    # pg_trgm не дропаем — может использоваться другими (cred/proxy поиски).
