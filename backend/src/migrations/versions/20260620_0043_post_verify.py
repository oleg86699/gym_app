"""Валидация ссылки в опубликованном посте:
- posting_runs.post_verify: 'mark' (отметка ✓/✗) | 'auto' (перепост пока не подтвердится).
- text_items.link_verified: NULL=не проверяли, true=ссылка есть, false=нет.
- text_items.verify_attempts: счётчик проверок/перепостов (auto).
verified_at / posted_url уже есть — переиспользуем (момент подтверждения + резолвленный permalink).
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_post_verify"
down_revision = "0042_texts_parent_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posting_runs", sa.Column(
        "post_verify", sa.String(8), nullable=False, server_default="mark"))
    op.add_column("text_items", sa.Column("link_verified", sa.Boolean(), nullable=True))
    op.add_column("text_items", sa.Column(
        "verify_attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("text_items", "verify_attempts")
    op.drop_column("text_items", "link_verified")
    op.drop_column("posting_runs", "post_verify")
