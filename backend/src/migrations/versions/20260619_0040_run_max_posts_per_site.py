"""Перенос max_posts_per_site с проекта на задачу (run).

Лимит «сколько раз один WP-сайт можно использовать» теперь задаётся per-run
(default 1), а не per-project. Воркер читает live-значение задачи — менеджер
может поднять лимит на уже созданной/завершённой задаче, чтобы «добрать» сайты
и до-запустить недопостившиеся айтемы.

- posting_runs.max_posts_per_site INT NOT NULL DEFAULT 1
- projects.max_posts_per_site — удаляем (больше не используется)
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_run_max_posts_per_site"
down_revision = "0039_ai_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column(
            "max_posts_per_site",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.drop_column("projects", "max_posts_per_site")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "max_posts_per_site",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.drop_column("posting_runs", "max_posts_per_site")
