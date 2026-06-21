"""text_items.gen_row JSONB — gen-контекст строки файла для пер-айтем
(ре)генерации csv_campaign (keyword/language/link/anchor). Нужен, чтобы по
кнопке «сгенерировать/перегенерировать» заново отрендерить промпт.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0041_text_item_gen_row"
down_revision = "0040_run_max_posts_per_site"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "text_items",
        sa.Column("gen_row", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("text_items", "gen_row")
