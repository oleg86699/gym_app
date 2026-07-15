"""app_settings.batch_validation_concurrency — per-batch cred concurrency

Рантайм-настройка скорости валидации: сколько кредов гоняется одновременно
внутри одного батча (per-batch семафор). Раньше бралось из env
DEFAULT_VALIDATION_CONCURRENCY (=5) — теперь тюнится без рестарта из настроек.
CF-браузеры остаются под потолком cf_browser_concurrency.
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_batch_validation_concurrency"
down_revision = "0057_max_concurrent_link_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("batch_validation_concurrency", sa.Integer(),
                  nullable=False, server_default="20"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "batch_validation_concurrency")
