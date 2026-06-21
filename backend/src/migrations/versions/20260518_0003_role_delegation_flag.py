"""Add is_assignable_by_group_admin flag on admin_roles.

Revision ID: 0003_role_delegation
Revises: 0002_projects
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_role_delegation"
down_revision: str | Sequence[str] | None = "0002_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "admin_roles",
        sa.Column(
            "is_assignable_by_group_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Дефолтная роль 'user' автоматически делегируется group_admin-у.
    op.execute("UPDATE admin_roles SET is_assignable_by_group_admin = true WHERE name = 'user'")


def downgrade() -> None:
    op.drop_column("admin_roles", "is_assignable_by_group_admin")
