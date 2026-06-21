"""RBAC initial: admin_users, groups, roles, permissions, pages + pivots.

Revision ID: 0001_rbac
Revises:
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_rbac"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── admin_groups ─────────────────────────────────────────────
    op.create_table(
        "admin_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_admin_groups_name", "admin_groups", ["name"], unique=True)
    op.create_index("ix_admin_groups_deleted_at", "admin_groups", ["deleted_at"])

    # ─── admin_roles ──────────────────────────────────────────────
    op.create_table(
        "admin_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_roles_name", "admin_roles", ["name"], unique=True)

    # ─── admin_permissions ────────────────────────────────────────
    op.create_table(
        "admin_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("resource", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("resource", "action", name="ux_perm_resource_action"),
    )
    op.create_index("ix_admin_permissions_code", "admin_permissions", ["code"], unique=True)

    # ─── admin_pages ──────────────────────────────────────────────
    op.create_table(
        "admin_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.String(200), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_pages_path", "admin_pages", ["path"], unique=True)

    # ─── admin_users ──────────────────────────────────────────────
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("admin_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_admin_users_username", "admin_users", ["username"], unique=True)
    op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)
    op.create_index("ix_admin_users_group_id", "admin_users", ["group_id"])
    op.create_index("ix_admin_users_deleted_at", "admin_users", ["deleted_at"])

    # ─── Pivot tables ─────────────────────────────────────────────
    op.create_table(
        "user_roles",
        sa.Column("admin_user_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("admin_roles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("admin_roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("admin_permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "role_pages",
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("admin_roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("page_id", sa.Integer(), sa.ForeignKey("admin_pages.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "user_pages",
        sa.Column("admin_user_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("page_id", sa.Integer(), sa.ForeignKey("admin_pages.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("user_pages")
    op.drop_table("role_pages")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_index("ix_admin_users_deleted_at", "admin_users")
    op.drop_index("ix_admin_users_group_id", "admin_users")
    op.drop_index("ix_admin_users_email", "admin_users")
    op.drop_index("ix_admin_users_username", "admin_users")
    op.drop_table("admin_users")
    op.drop_index("ix_admin_pages_path", "admin_pages")
    op.drop_table("admin_pages")
    op.drop_index("ix_admin_permissions_code", "admin_permissions")
    op.drop_table("admin_permissions")
    op.drop_index("ix_admin_roles_name", "admin_roles")
    op.drop_table("admin_roles")
    op.drop_index("ix_admin_groups_deleted_at", "admin_groups")
    op.drop_index("ix_admin_groups_name", "admin_groups")
    op.drop_table("admin_groups")
