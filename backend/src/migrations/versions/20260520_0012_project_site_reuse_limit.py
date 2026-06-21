"""projects.max_posts_per_site + non-unique index на project_wp_used.

Сейчас уникальный констрэйнт (project_id, site_id) защищал «1 site = 1 проект».
Меняем модель: лимит per project задаёт сам менеджер (по умолчанию 1, можно
больше). Снимаем UNIQUE, кладём non-unique индекс для COUNT(*) lookup-а.

Revision ID: 0012_project_reuse_limit
Revises: 0011_publish_window
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_project_reuse_limit"
down_revision: str | Sequence[str] | None = "0011_publish_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # projects.max_posts_per_site (default 1 — сохраняет старое поведение)
    op.add_column(
        "projects",
        sa.Column(
            "max_posts_per_site",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # Снимаем UNIQUE, ставим non-unique индекс для COUNT(*)-lookup-ов воркера
    op.drop_constraint("ux_project_site_used", "project_wp_used", type_="unique")
    op.create_index(
        "ix_project_wp_used_project_site",
        "project_wp_used",
        ["project_id", "site_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_wp_used_project_site", table_name="project_wp_used")
    # Если в таблице уже есть дубликаты (project_id, site_id) — UNIQUE не создастся.
    # Это сознательно: downgrade на проде потребует ручной дедуп.
    op.create_unique_constraint(
        "ux_project_site_used", "project_wp_used", ["project_id", "site_id"]
    )
    op.drop_column("projects", "max_posts_per_site")
