"""Posting core: wp_accesses, posting_runs, text_items, project_wp_used, run_artifacts.

Revision ID: 0005_posting_core
Revises: 0004_invitations
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_posting_core"
down_revision: str | Sequence[str] | None = "0004_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── wp_accesses ────────────────────────────────────────────────
    op.create_table(
        "wp_accesses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("login", sa.String(255), nullable=False),
        sa.Column("password", sa.String(500), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error_counter", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("amount_use", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_filename", sa.String(500), nullable=True),
        sa.Column("tag", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("domain", "login", name="ux_wp_accesses_domain_login"),
    )
    op.create_index("ix_wp_accesses_domain", "wp_accesses", ["domain"])
    op.create_index("ix_wp_accesses_tag", "wp_accesses", ["tag"])
    op.create_index("ix_wp_accesses_deleted_at", "wp_accesses", ["deleted_at"])
    # partial index для горячего запроса «дай валидную свободную админку»
    op.create_index(
        "ix_wp_accesses_valid",
        "wp_accesses",
        ["error_counter"],
        postgresql_where=sa.text("is_valid = true AND deleted_at IS NULL"),
    )

    # ─── posting_runs ───────────────────────────────────────────────
    op.create_table(
        "posting_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=False, server_default=sa.text("45")),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default=sa.text("25")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("total_texts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("posted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("proxy_id", sa.Integer(), nullable=True),
        sa.Column("source_archive_storage_key", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_posting_runs_project_id", "posting_runs", ["project_id"])
    op.create_index("ix_posting_runs_created_by", "posting_runs", ["created_by"])
    op.create_index("ix_posting_runs_project_status", "posting_runs", ["project_id", "status"])
    op.create_index("ix_posting_runs_status_scheduled", "posting_runs", ["status", "scheduled_for"])

    # Агрессивный autovacuum — на posting_runs идут частые UPDATE счётчиков (ADR-011)
    op.execute(
        """
        ALTER TABLE posting_runs SET (
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_analyze_scale_factor = 0.01,
            autovacuum_vacuum_cost_delay = 10,
            fillfactor = 80
        )
        """
    )

    # ─── text_items ────────────────────────────────────────────────
    op.create_table(
        "text_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("posting_run_id", sa.Integer(),
                  sa.ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=True),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("title", sa.String(1000), nullable=True),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("posted_url", sa.Text(), nullable=True),
        sa.Column("post_id", sa.BigInteger(), nullable=True),
        sa.Column("wp_access_id", sa.Integer(),
                  sa.ForeignKey("wp_accesses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_text_items_posting_run_id", "text_items", ["posting_run_id"])
    op.create_index("ix_text_items_project_id", "text_items", ["project_id"])
    op.create_index("ix_text_items_content_hash", "text_items", ["content_hash"])
    op.create_index("ix_text_items_wp_access_id", "text_items", ["wp_access_id"])
    op.create_index("ix_text_items_run_status", "text_items", ["posting_run_id", "status"])
    op.create_index("ix_text_items_project_status", "text_items", ["project_id", "status"])

    # autovacuum + fillfactor (часто UPDATE-ятся status/posted_url/wp_access_id)
    op.execute(
        """
        ALTER TABLE text_items SET (
            autovacuum_vacuum_scale_factor = 0.05,
            autovacuum_analyze_scale_factor = 0.02,
            fillfactor = 80
        )
        """
    )

    # ─── project_wp_used ───────────────────────────────────────────
    op.create_table(
        "project_wp_used",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wp_access_id", sa.Integer(),
                  sa.ForeignKey("wp_accesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("posting_run_id", sa.Integer(),
                  sa.ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text_item_id", sa.Integer(),
                  sa.ForeignKey("text_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "wp_access_id", name="ux_project_wp_used"),
    )
    op.create_index("ix_project_wp_used_run", "project_wp_used", ["posting_run_id"])

    op.execute(
        """
        ALTER TABLE project_wp_used SET (
            autovacuum_vacuum_scale_factor = 0.1
        )
        """
    )

    # ─── run_artifacts ─────────────────────────────────────────────
    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("posting_run_id", sa.Integer(),
                  sa.ForeignKey("posting_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_run_artifacts_posting_run_id", "run_artifacts", ["posting_run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_artifacts_posting_run_id", "run_artifacts")
    op.drop_table("run_artifacts")

    op.drop_index("ix_project_wp_used_run", "project_wp_used")
    op.drop_table("project_wp_used")

    for ix in (
        "ix_text_items_project_status",
        "ix_text_items_run_status",
        "ix_text_items_wp_access_id",
        "ix_text_items_content_hash",
        "ix_text_items_project_id",
        "ix_text_items_posting_run_id",
    ):
        op.drop_index(ix, "text_items")
    op.drop_table("text_items")

    for ix in (
        "ix_posting_runs_status_scheduled",
        "ix_posting_runs_project_status",
        "ix_posting_runs_created_by",
        "ix_posting_runs_project_id",
    ):
        op.drop_index(ix, "posting_runs")
    op.drop_table("posting_runs")

    for ix in (
        "ix_wp_accesses_valid",
        "ix_wp_accesses_deleted_at",
        "ix_wp_accesses_tag",
        "ix_wp_accesses_domain",
    ):
        op.drop_index(ix, "wp_accesses")
    op.drop_table("wp_accesses")
