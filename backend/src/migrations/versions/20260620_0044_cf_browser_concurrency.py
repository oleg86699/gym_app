"""app_settings.cf_browser_concurrency — кап одновременных браузер-контекстов
(Patchright) для CF Tier 3. Тюнится в /settings под RAM сервера."""
from alembic import op
import sqlalchemy as sa

revision = "0044_cf_browser_concurrency"
down_revision = "0043_post_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column(
        "cf_browser_concurrency", sa.Integer(), nullable=False, server_default="3"))


def downgrade() -> None:
    op.drop_column("app_settings", "cf_browser_concurrency")
