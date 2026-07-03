"""Ограничение одновременных валидаций батчей + очередь.

app_settings.max_concurrent_batch_validations — сколько батчей валидируется
разом (остальные ждут в статусе 'queued'). wp_import_batches.queued_validation_params
— параметры отложенной валидации (scope/provision/actor), чтобы диспетчер поднял
батч ровно как задумано.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0053_batch_validation_queue"
down_revision = "0052_tag_access_allowlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column(
        "max_concurrent_batch_validations", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("wp_import_batches", sa.Column(
        "queued_validation_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("wp_import_batches", "queued_validation_params")
    op.drop_column("app_settings", "max_concurrent_batch_validations")
