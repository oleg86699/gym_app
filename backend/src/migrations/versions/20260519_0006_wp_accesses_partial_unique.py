"""Заменяет UNIQUE(domain, login) на partial-unique WHERE deleted_at IS NULL.

Иначе soft-deleted записи блокируют повторное добавление того же доступа
(на тот же домен с тем же логином).

Revision ID: 0006_wp_partial_uniq
Revises: 0005_posting_core
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_wp_partial_uniq"
down_revision: str | Sequence[str] | None = "0005_posting_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ux_wp_accesses_domain_login", "wp_accesses", type_="unique")
    op.create_index(
        "ux_wp_accesses_domain_login",
        "wp_accesses",
        ["domain", "login"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_wp_accesses_domain_login", "wp_accesses")
    op.create_unique_constraint(
        "ux_wp_accesses_domain_login", "wp_accesses", ["domain", "login"]
    )
