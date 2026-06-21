"""Audit log table.

Revision ID: 0016_audit_log
Revises: 0015_proxies
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016_audit_log"
down_revision: str | Sequence[str] | None = "0015_proxies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("changes", JSONB(), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_log_created", "audit_log", ["created_at"])
    op.create_index(
        "ix_audit_log_actor_created", "audit_log", ["actor_user_id", "created_at"]
    )
    op.create_index(
        "ix_audit_log_action_created", "audit_log", ["action", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_created", table_name="audit_log")
    op.drop_index("ix_audit_log_created", table_name="audit_log")
    op.drop_table("audit_log")
