"""WP Import Batches + cred extension (tags array, cooldown, batch_id, lang_detected_at).

- Создаём `wp_import_batches`.
- Добавляем `wp_credentials.tags TEXT[]` (миграция из старого `tag VARCHAR`).
- Добавляем cooldown поля: `last_error_at`, `error_cooldown_until`, `last_successful_post_at`.
- Добавляем `wp_credentials.import_batch_id`.
- Добавляем `wp_sites.language_detected_at`.

Revision ID: 0017_wp_batches
Revises: 0016_audit_log
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0017_wp_batches"
down_revision: str | Sequence[str] | None = "0016_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── wp_import_batches ──────────────────────────────────────────────
    op.create_table(
        "wp_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("tag", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("cost_total", sa.Numeric(12, 2), nullable=True),
        sa.Column("cost_currency", sa.String(8), nullable=True),
        sa.Column("source_filename", sa.String(500), nullable=True),
        sa.Column("file_storage_key", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploaded"),
        sa.Column("total_credentials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_credentials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("validation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validation_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wp_batches_status_created", "wp_import_batches", ["status", "created_at"])
    op.create_index("ix_wp_batches_deleted_at", "wp_import_batches", ["deleted_at"])

    # ── wp_credentials расширения ─────────────────────────────────────
    op.add_column("wp_credentials", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("wp_credentials", sa.Column("error_cooldown_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("wp_credentials", sa.Column("last_successful_post_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "wp_credentials",
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_wp_cred_batch", "wp_credentials", "wp_import_batches",
        ["import_batch_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_wp_cred_import_batch", "wp_credentials", ["import_batch_id"])

    # tags: array column, backfill from old `tag`
    op.add_column("wp_credentials", sa.Column("tags", ARRAY(sa.String(100)), nullable=True))
    op.execute("UPDATE wp_credentials SET tags = ARRAY[tag] WHERE tag IS NOT NULL")
    op.drop_index("ix_wp_credentials_tag", table_name="wp_credentials")
    op.drop_column("wp_credentials", "tag")
    op.create_index("ix_wp_cred_tags_gin", "wp_credentials", ["tags"], postgresql_using="gin")

    # ── wp_sites.language_detected_at ─────────────────────────────────
    op.add_column(
        "wp_sites",
        sa.Column("language_detected_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wp_sites", "language_detected_at")

    op.drop_index("ix_wp_cred_tags_gin", table_name="wp_credentials")
    # Backfill: tags[0] → tag
    op.add_column("wp_credentials", sa.Column("tag", sa.String(100), nullable=True))
    op.execute(
        "UPDATE wp_credentials SET tag = tags[1] WHERE tags IS NOT NULL AND array_length(tags, 1) >= 1"
    )
    op.create_index("ix_wp_credentials_tag", "wp_credentials", ["tag"])
    op.drop_column("wp_credentials", "tags")

    op.drop_index("ix_wp_cred_import_batch", table_name="wp_credentials")
    op.drop_constraint("fk_wp_cred_batch", "wp_credentials", type_="foreignkey")
    op.drop_column("wp_credentials", "import_batch_id")
    op.drop_column("wp_credentials", "last_successful_post_at")
    op.drop_column("wp_credentials", "error_cooldown_until")
    op.drop_column("wp_credentials", "last_error_at")

    op.drop_index("ix_wp_batches_deleted_at", table_name="wp_import_batches")
    op.drop_index("ix_wp_batches_status_created", table_name="wp_import_batches")
    op.drop_table("wp_import_batches")
