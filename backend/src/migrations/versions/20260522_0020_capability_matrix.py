"""Capability matrix per cred × site.

Расширяем модель: вместо binary `is_valid` теперь cred имеет набор
капабилити-флагов — что именно работает на сайте через эту cred.

Идея: одна валидация (или реальный постинг) выставляет несколько флагов
сразу, и каждый последующий запрос знает что пробовать (XML-RPC ушёл? →
сразу Tier 2 admin), а что точно мёртвое и не дёргать.

На WpCredential:
  can_xmlrpc, can_admin_login, can_post_via_xmlrpc, can_post_via_admin,
  can_edit_pages, can_edit_themes, can_edit_widgets,
  admin_role, last_admin_check_at

На WpSite:
  cf_protected, wp_version, active_theme,
  file_editing_disabled, homepage_is_static_page, homepage_page_id

Все nullable — None означает «ещё не проверяли», False = «проверили, не
работает», True = «работает».

Revision ID: 0020_capability_matrix
Revises: 0019_site_failure_tracking
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_capability_matrix"
down_revision: str | Sequence[str] | None = "0019_site_failure_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── WpCredential: что эта cred умеет на этом сайте ─────────────
    op.add_column("wp_credentials", sa.Column("can_xmlrpc", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_admin_login", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_post_via_xmlrpc", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_post_via_admin", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_edit_pages", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_edit_themes", sa.Boolean, nullable=True))
    op.add_column("wp_credentials", sa.Column("can_edit_widgets", sa.Boolean, nullable=True))
    # WP capability/role: 'administrator' | 'editor' | 'author' | 'contributor' | ...
    op.add_column("wp_credentials", sa.Column("admin_role", sa.String(50), nullable=True))
    # Когда последний раз гоняли Tier 2 (admin form-login + capability probes)
    op.add_column(
        "wp_credentials",
        sa.Column("last_admin_check_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ─── WpSite: что про сам сайт известно ──────────────────────────
    op.add_column("wp_sites", sa.Column("cf_protected", sa.Boolean, nullable=True))
    op.add_column("wp_sites", sa.Column("wp_version", sa.String(32), nullable=True))
    op.add_column("wp_sites", sa.Column("active_theme", sa.String(120), nullable=True))
    # Detected via "File editing is disabled" / DISALLOW_FILE_EDIT
    op.add_column("wp_sites", sa.Column("file_editing_disabled", sa.Boolean, nullable=True))
    # Settings → Reading: show_on_front=page
    op.add_column("wp_sites", sa.Column("homepage_is_static_page", sa.Boolean, nullable=True))
    op.add_column("wp_sites", sa.Column("homepage_page_id", sa.Integer, nullable=True))


def downgrade() -> None:
    # WpSite
    op.drop_column("wp_sites", "homepage_page_id")
    op.drop_column("wp_sites", "homepage_is_static_page")
    op.drop_column("wp_sites", "file_editing_disabled")
    op.drop_column("wp_sites", "active_theme")
    op.drop_column("wp_sites", "wp_version")
    op.drop_column("wp_sites", "cf_protected")
    # WpCredential
    op.drop_column("wp_credentials", "last_admin_check_at")
    op.drop_column("wp_credentials", "admin_role")
    op.drop_column("wp_credentials", "can_edit_widgets")
    op.drop_column("wp_credentials", "can_edit_themes")
    op.drop_column("wp_credentials", "can_edit_pages")
    op.drop_column("wp_credentials", "can_post_via_admin")
    op.drop_column("wp_credentials", "can_post_via_xmlrpc")
    op.drop_column("wp_credentials", "can_admin_login")
    op.drop_column("wp_credentials", "can_xmlrpc")
