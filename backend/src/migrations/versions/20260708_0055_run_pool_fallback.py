"""pool_fallback: авто-добор по полному разрешённому пулу.

posting_runs.pool_fallback — если фильтрованный пул (lang/TLD/tags/domains)
исчерпан, вместо need_more_admins продолжить постинг по всему остальному
разрешённому пользователю пулу (в рамках RBAC-тегов создателя).
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_run_pool_fallback"
down_revision = "0054_run_proxy_fallback_direct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posting_runs", sa.Column(
        "pool_fallback", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("posting_runs", "pool_fallback")
