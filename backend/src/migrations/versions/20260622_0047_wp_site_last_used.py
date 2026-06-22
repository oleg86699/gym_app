"""wp_sites.last_used_at — метка последнего успешного поста на сайт, для
LRU-отбора кандидатов (_pick_candidate_sites: last_used_at NULLS FIRST, random()).
Ровный делёж бэклинков по пулу. Бэкафилл из истории project_wp_used."""
from alembic import op
import sqlalchemy as sa

revision = "0047_wp_site_last_used"
down_revision = "0046_posting_tuning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wp_sites",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Сидируем LRU из истории использований: последний раз, когда сайт занимали
    # в любом проекте. Никогда не использованные остаются NULL (берутся первыми).
    op.execute(
        """
        UPDATE wp_sites s
        SET last_used_at = u.last_used
        FROM (
            SELECT site_id, MAX(created_at) AS last_used
            FROM project_wp_used
            GROUP BY site_id
        ) u
        WHERE u.site_id = s.id
        """
    )


def downgrade() -> None:
    op.drop_column("wp_sites", "last_used_at")
