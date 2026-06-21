"""WpCredential.can_create_users — право создавать пользователей (REST users/me).

Захватываем из /wp-json/wp/v2/users/me?context=edit при validation (medium).
Нужно для будущего provision-author (создать своего author может только тот
у кого create_users=true, т.е. administrator).

`admin_role` уже есть (миграция 0020). Здесь добавляем явный capability-флаг.

Revision ID: 0030_cred_can_create_users
Revises: 0029_site_events
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_cred_can_create_users"
down_revision = "0029_site_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wp_credentials",
        sa.Column("can_create_users", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wp_credentials", "can_create_users")
