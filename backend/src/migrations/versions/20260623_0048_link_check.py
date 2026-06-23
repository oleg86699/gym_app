"""posting_runs: состояние перепроверки проставленных бэклинков (link-check).
Отдельная фоновая задача (TaskIQ), запускается вручную после завершения постинга,
перепроверяет уже валидные ссылки (link_verified=true). Видна в глобальной очереди
как фиолетовый тип. Колонки: status (NULL|queued|running|done), total/done/valid, at."""
from alembic import op
import sqlalchemy as sa

revision = "0048_link_check"
down_revision = "0047_wp_site_last_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posting_runs", sa.Column("link_check_status", sa.String(16), nullable=True))
    op.add_column(
        "posting_runs",
        sa.Column("link_check_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "posting_runs",
        sa.Column("link_check_done", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "posting_runs",
        sa.Column("link_check_valid", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "posting_runs",
        sa.Column("link_check_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("posting_runs", "link_check_at")
    op.drop_column("posting_runs", "link_check_valid")
    op.drop_column("posting_runs", "link_check_done")
    op.drop_column("posting_runs", "link_check_total")
    op.drop_column("posting_runs", "link_check_status")
