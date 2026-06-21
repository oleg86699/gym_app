"""Projects + access scope: projects, group_projects, user_projects.

Revision ID: 0002_projects
Revises: 0001_rbac
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_projects"
down_revision: str | Sequence[str] | None = "0001_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_group_id",
            sa.Integer(),
            sa.ForeignKey("admin_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_projects_name", "projects", ["name"])
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])
    op.create_index("ix_projects_owner_group_id", "projects", ["owner_group_id"])
    op.create_index("ix_projects_deleted_at", "projects", ["deleted_at"])

    op.create_table(
        "group_projects",
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("admin_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "user_projects",
        sa.Column(
            "admin_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_projects")
    op.drop_table("group_projects")
    op.drop_index("ix_projects_deleted_at", "projects")
    op.drop_index("ix_projects_owner_group_id", "projects")
    op.drop_index("ix_projects_owner_user_id", "projects")
    op.drop_index("ix_projects_name", "projects")
    op.drop_table("projects")
