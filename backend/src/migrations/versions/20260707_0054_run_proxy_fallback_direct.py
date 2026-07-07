"""Пре-флайт прокси: флаг proxy_fallback_direct на прогоне.

posting_runs.proxy_fallback_direct — True, если на старте рана пул прокси
оказался в основном мёртв (напр. забыли оплатить) и воркер ушёл в direct.
Нужен для UI-статуса «постинг идёт напрямую».
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_run_proxy_fallback_direct"
down_revision = "0053_batch_validation_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posting_runs", sa.Column(
        "proxy_fallback_direct", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("posting_runs", "proxy_fallback_direct")
