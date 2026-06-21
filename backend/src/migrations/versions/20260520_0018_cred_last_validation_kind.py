"""wp_credentials.last_validation_kind + last_error_message.

Без этого UI badge не отличает «подтверждено OK» от «была transient
ошибка» от «никогда не валидировали», и юзер не видит причину
не-валидности.

Revision ID: 0018_cred_validation_kind
Revises: 0017_wp_batches
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_cred_validation_kind"
down_revision: str | Sequence[str] | None = "0017_wp_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wp_credentials",
        sa.Column("last_validation_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "wp_credentials",
        sa.Column("last_error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wp_credentials", "last_error_message")
    op.drop_column("wp_credentials", "last_validation_kind")
