"""PostingRun.proxy_selector — пул прокси вместо одиночного proxy_id.

Раньше run.proxy_id (FK на одну прокси) — bottleneck при 1000+ текстов:
один IP не справляется со всем нагрузкой, попадает в rate-limit плагинов
WP/Wordfence/cf, и run застревает.

Теперь — строковый селектор:
  - "direct"           — без прокси
  - "all"              — все active прокси, round-robin
  - "provider:<name>"  — все active прокси этого провайдера (webshare/decodo/...)
  - "single:<id>"      — один конкретный proxy (старое поведение, для отладки)

Worker при каждом запросе берёт случайную (или round-robin) прокси из пула.
Если все прокси пула умерли — fallback на direct + флаг warning в run state.

proxy_id оставляем nullable для back-compat: старые runs без selector
используют его как было (single proxy).

Revision ID: 0022_proxy_pool
Revises: 0021_posting_method
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_proxy_pool"
down_revision: str | Sequence[str] | None = "0021_posting_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column("proxy_selector", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("posting_runs", "proxy_selector")
