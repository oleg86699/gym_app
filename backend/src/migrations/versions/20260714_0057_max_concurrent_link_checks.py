"""app_settings.max_concurrent_link_checks — лимит одновременных link-check

Очередь для перепроверки ссылок (как у батч-валидации): не более N проверок
одновременно, остальные ждут в link_check_status='queued'.
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_max_concurrent_link_checks"
down_revision = "0056_text_item_link_html"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("max_concurrent_link_checks", sa.Integer(),
                  nullable=False, server_default="2"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "max_concurrent_link_checks")
