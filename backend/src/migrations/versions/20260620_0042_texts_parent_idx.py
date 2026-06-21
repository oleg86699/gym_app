"""Индекс texts.parent_text_id — для подсчёта спин-вариантов оригинала
(spin_count) в библиотеке текстов и любых reuse-выборок «дети оригинала».
FK не создаёт индекс автоматически, а выборка `parent_text_id = ANY(:ids)`
иначе делает seq-scan по texts.
"""
from alembic import op

revision = "0042_texts_parent_idx"
down_revision = "0041_text_item_gen_row"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_texts_parent_text_id", "texts", ["parent_text_id"])


def downgrade() -> None:
    op.drop_index("ix_texts_parent_text_id", table_name="texts")
