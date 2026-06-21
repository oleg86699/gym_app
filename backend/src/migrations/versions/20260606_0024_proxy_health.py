"""Proxy health: consecutive_failures + locked_until.

Аналогично site_failure_tracking (миграция 0019): прокси накапливает
network-fail-ы; при достижении threshold лочится на cooldown. Иначе один
мёртвый прокси из pool=10 портит 10% всех запросов до ручного вмешательства.

Поведение:
  - Любая сетевая ошибка через прокси → consecutive_failures++
  - При consecutive_failures >= PROXY_FAILURE_THRESHOLD (5) → locked_until = now + 30 min
  - Pool селектор пропускает прокси с locked_until > now()
  - Любой успешный запрос через прокси → reset counter

Revision ID: 0024_proxy_health
Revises: 0023_batch_duplicate_cred_ids
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_proxy_health"
down_revision = "0023_batch_duplicate_cred_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proxies",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "proxies",
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "proxies",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Композитный индекс для hot-path селектора (фильтр в WHERE).
    # `now()` в partial-index невозможен (PG требует IMMUTABLE), поэтому
    # индексируем (is_active, locked_until) и фильтруем locked_until в query.
    op.create_index(
        "ix_proxies_active_locked",
        "proxies",
        ["is_active", "locked_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_proxies_active_locked", table_name="proxies")
    op.drop_column("proxies", "locked_until")
    op.drop_column("proxies", "last_failure_at")
    op.drop_column("proxies", "consecutive_failures")
