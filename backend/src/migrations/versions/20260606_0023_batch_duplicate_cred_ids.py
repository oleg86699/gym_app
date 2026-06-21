"""WpImportBatch.duplicate_cred_ids — список ID оригиналов-дублей при импорте.

При импорте CSV cred c (site_id, login) уже существующим в БД пропускается
через ON CONFLICT DO NOTHING и нигде не сохраняется. Counter
`duplicate_credentials` показывал только число, но не отвечал на вопрос
«какие именно были дублями».

Теперь после INSERT находим оригиналы (cred в других batches с теми же
парами) и сохраняем их IDs в JSONB-массиве. Filter='duplicates' просто
читает этот список. Без засорения БД лишними cred и без хитрых ad-hoc
JOIN запросов на каждый клик.

JSONB вместо ARRAY(BigInt) — гибче, легко мигрировать формат позже
(добавить например {id, note: "imported as alice@..."}).

Revision ID: 0023_batch_duplicate_cred_ids
Revises: 0022_proxy_pool
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_batch_duplicate_cred_ids"
down_revision = "0022_proxy_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wp_import_batches",
        sa.Column(
            "duplicate_cred_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("wp_import_batches", "duplicate_cred_ids")
