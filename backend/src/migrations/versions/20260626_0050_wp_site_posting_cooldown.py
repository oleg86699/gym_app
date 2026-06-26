"""wp_sites.posting_cooldown_until — временный лок сайта на постинг.

Транзиент-фейл (503/415/429/network/CF/timeout) ставит сайт на cooldown: он
выпадает из пула на постинг до истечения, не блокируя задачу (та встаёт в
need_more_admins вместо бесконечного грайнда по тем же транзиент-сайтам).
Постоянное выключение — отдельно через consecutive_site_failures ≥ порог."""
from alembic import op
import sqlalchemy as sa

revision = "0050_wp_site_posting_cooldown"
down_revision = "0049_supplier_temp_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wp_sites",
        sa.Column("posting_cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Частичный индекс — быстрый фильтр «не залочен» в подборе кандидатов.
    op.create_index(
        "ix_wp_sites_posting_cooldown_until",
        "wp_sites",
        ["posting_cooldown_until"],
        postgresql_where=sa.text("posting_cooldown_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_wp_sites_posting_cooldown_until", table_name="wp_sites")
    op.drop_column("wp_sites", "posting_cooldown_until")
