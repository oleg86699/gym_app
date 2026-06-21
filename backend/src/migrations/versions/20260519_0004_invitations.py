"""Invitations table.

Revision ID: 0004_invitations
Revises: 0003_role_delegation
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0004_invitations"
down_revision: str | Sequence[str] | None = "0003_role_delegation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("admin_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("role_ids", ARRAY(sa.Integer()), nullable=False, server_default="{}"),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "used_by_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_invitations_token_hash", "invitations", ["token_hash"], unique=True)
    op.create_index("ix_invitations_created_by_user_id", "invitations", ["created_by_user_id"])
    op.create_index("ix_invitations_group_id", "invitations", ["group_id"])
    op.create_index("ix_invitations_expires_at", "invitations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_invitations_expires_at", "invitations")
    op.drop_index("ix_invitations_group_id", "invitations")
    op.drop_index("ix_invitations_created_by_user_id", "invitations")
    op.drop_index("ix_invitations_token_hash", "invitations")
    op.drop_table("invitations")
