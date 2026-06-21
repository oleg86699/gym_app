"""Реструктура: wp_accesses → wp_sites + wp_credentials.

Сайт (один на bare-домен) + credentials (много логинов на сайт).
Постинг привязан к сайту (1 site = 1 проект), credential — детали кто
именно постил.

Revision ID: 0008_wp_sites_creds
Revises: 0007_wp_working_url
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_wp_sites_creds"
down_revision: str | Sequence[str] | None = "0007_wp_working_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── 1. Новые таблицы ────────────────────────────────────────

    op.create_table(
        "wp_sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("hint_path", sa.String(200), nullable=True),
        sa.Column("hint_port", sa.Integer(), nullable=True),
        sa.Column("last_working_url", sa.String(500), nullable=True),
        sa.Column("last_working_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wp_sites_domain", "wp_sites", ["domain"])
    op.create_index("ix_wp_sites_deleted_at", "wp_sites", ["deleted_at"])
    op.create_index(
        "ux_wp_sites_domain",
        "wp_sites",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "wp_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("wp_sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("login", sa.String(255), nullable=False),
        sa.Column("password", sa.String(500), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error_counter", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("amount_use", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_filename", sa.String(500), nullable=True),
        sa.Column("tag", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wp_credentials_site_id", "wp_credentials", ["site_id"])
    op.create_index("ix_wp_credentials_tag", "wp_credentials", ["tag"])
    op.create_index("ix_wp_credentials_deleted_at", "wp_credentials", ["deleted_at"])
    op.create_index(
        "ux_wp_credentials_site_login",
        "wp_credentials",
        ["site_id", "login"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ─── 2. Перенос данных из wp_accesses ─────────────────────────

    # Сначала уникальные домены → sites
    op.execute(
        """
        INSERT INTO wp_sites (
            domain, last_working_url, last_working_at,
            is_active, language, note, created_at, updated_at, deleted_at
        )
        SELECT
            domain,
            MAX(last_working_url),
            MAX(last_working_at),
            BOOL_OR(is_valid),
            MAX(language),
            STRING_AGG(DISTINCT note, ' | ') FILTER (WHERE note IS NOT NULL),
            MIN(created_at),
            MAX(updated_at),
            CASE WHEN BOOL_AND(deleted_at IS NOT NULL) THEN MIN(deleted_at) END
        FROM wp_accesses
        GROUP BY domain
        """
    )

    # Теперь credentials, привязанные к sites
    op.execute(
        """
        INSERT INTO wp_credentials (
            site_id, login, password, is_valid, error_counter, last_validated_at,
            amount_use, source_filename, tag, note,
            created_at, updated_at, deleted_at
        )
        SELECT
            s.id, a.login, a.password, a.is_valid, a.error_counter, a.last_validated_at,
            a.amount_use, a.source_filename, a.tag, a.note,
            a.created_at, a.updated_at, a.deleted_at
        FROM wp_accesses a
        JOIN wp_sites s ON s.domain = a.domain
        """
    )

    # ─── 3. text_items: wp_access_id → credential_id + site_id ──

    op.add_column("text_items", sa.Column("credential_id", sa.Integer(), nullable=True))
    op.add_column("text_items", sa.Column("site_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE text_items t
        SET credential_id = c.id,
            site_id = c.site_id
        FROM wp_accesses a
        JOIN wp_sites s ON s.domain = a.domain
        JOIN wp_credentials c ON c.site_id = s.id AND c.login = a.login
        WHERE t.wp_access_id = a.id
        """
    )
    op.drop_index("ix_text_items_wp_access_id", "text_items")
    op.drop_column("text_items", "wp_access_id")
    op.create_foreign_key(
        "fk_text_items_credential", "text_items", "wp_credentials",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_text_items_site", "text_items", "wp_sites",
        ["site_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_text_items_credential_id", "text_items", ["credential_id"])
    op.create_index("ix_text_items_site_id", "text_items", ["site_id"])

    # ─── 4. project_wp_used: wp_access_id → site_id + credential_id ──

    op.add_column("project_wp_used", sa.Column("site_id", sa.Integer(), nullable=True))
    op.add_column("project_wp_used", sa.Column("credential_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE project_wp_used p
        SET site_id = s.id,
            credential_id = c.id
        FROM wp_accesses a
        JOIN wp_sites s ON s.domain = a.domain
        JOIN wp_credentials c ON c.site_id = s.id AND c.login = a.login
        WHERE p.wp_access_id = a.id
        """
    )
    # Дедуп: если на один сайт было несколько wp_access с разными логинами —
    # после миграции остаётся ОДНА запись на (project, site). Оставим самую раннюю.
    op.execute(
        """
        DELETE FROM project_wp_used p
        USING project_wp_used p2
        WHERE p.project_id = p2.project_id
          AND p.site_id = p2.site_id
          AND p.site_id IS NOT NULL
          AND p.id > p2.id
        """
    )
    op.drop_constraint("ux_project_wp_used", "project_wp_used", type_="unique")
    op.drop_column("project_wp_used", "wp_access_id")
    op.alter_column("project_wp_used", "site_id", nullable=False)
    op.create_foreign_key(
        "fk_project_wp_used_site", "project_wp_used", "wp_sites",
        ["site_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_project_wp_used_credential", "project_wp_used", "wp_credentials",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "ux_project_site_used", "project_wp_used", ["project_id", "site_id"]
    )

    # ─── 5. Удалить старую wp_accesses ────────────────────────────

    op.drop_table("wp_accesses")


def downgrade() -> None:
    raise NotImplementedError("Структурные изменения этой миграции необратимы без потери данных")
