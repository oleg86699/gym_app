"""text_items.link_html — готовый HTML-сниппет для link-простановки

Позволяет ставить сквозную/homepage-ссылку как готовый HTML-текст (с уже
встроенной ссылкой и тегами) вместо авто-обёртки <a href=link>anchor</a>.
NULL = обычный режим (url+anchor).
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_text_item_link_html"
down_revision = "0055_run_pool_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("text_items", sa.Column("link_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("text_items", "link_html")
