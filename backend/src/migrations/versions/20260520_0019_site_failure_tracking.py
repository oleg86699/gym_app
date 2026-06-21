"""wp_sites: site-level failure tracking для авто-выключения мёртвых доменов.

Если по сайту подряд идут N network/server_error/site_not_found/xmlrpc_disabled
ответов — выключаем is_active. Реализуется счётчиком consecutive_site_failures
(сбрасывается на любой ответ XML-RPC: и ok, и auth-fail значит «сайт жив»).

Revision ID: 0019_site_failure_tracking
Revises: 0018_cred_validation_kind
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_site_failure_tracking"
down_revision: str | Sequence[str] | None = "0018_cred_validation_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wp_sites",
        sa.Column(
            "consecutive_site_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "wp_sites",
        sa.Column("last_site_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wp_sites",
        sa.Column("last_site_failure_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "wp_sites",
        sa.Column("auto_disabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wp_sites", "auto_disabled_at")
    op.drop_column("wp_sites", "last_site_failure_kind")
    op.drop_column("wp_sites", "last_site_failure_at")
    op.drop_column("wp_sites", "consecutive_site_failures")
