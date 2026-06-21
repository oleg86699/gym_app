"""Фаза A: project_domains + разбор ссылок/анкоров + язык на text_items.

- project_domains: целевые (money) домены проекта; по ним из текстов вытаскиваем
  «наши» бэклинки и строим аналитику.
- text_items.target_domain: нормализованный домен из link_url (аналитика).
- text_items.link_candidates: jsonb извлечённых ссылок-кандидатов (для UI выбора
  при needs_review).
- text_items.lang: язык текста (langdetect на заливке; для будущего поиска).

Статусы needs_review (item) и needs_review (run) — это новые строковые значения
в varchar-колонках status, отдельной DDL не требуют.

Revision ID: 0035_project_domains_links
Revises: 0034_drip_not_before
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_project_domains_links"
down_revision = "0034_drip_not_before"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_domains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "domain", name="uq_project_domain"),
    )
    op.create_index("ix_project_domains_project_id", "project_domains", ["project_id"])
    op.create_index("ix_project_domains_domain", "project_domains", ["domain"])

    op.add_column("text_items", sa.Column("target_domain", sa.String(length=255), nullable=True))
    op.add_column("text_items", sa.Column("link_candidates", postgresql.JSONB(), nullable=True))
    op.add_column("text_items", sa.Column("lang", sa.String(length=10), nullable=True))
    op.create_index("ix_text_items_target_domain", "text_items", ["target_domain"])


def downgrade() -> None:
    op.drop_index("ix_text_items_target_domain", table_name="text_items")
    op.drop_column("text_items", "lang")
    op.drop_column("text_items", "link_candidates")
    op.drop_column("text_items", "target_domain")
    op.drop_index("ix_project_domains_domain", table_name="project_domains")
    op.drop_index("ix_project_domains_project_id", table_name="project_domains")
    op.drop_table("project_domains")
