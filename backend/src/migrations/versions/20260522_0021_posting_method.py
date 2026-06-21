"""PostingRun.posting_method — выбор канала постинга (XML-RPC / wp-admin / auto).

- `auto` (default) — Tier 1 (XML-RPC) первым; на XMLRPC_DISABLED → Tier 2 (admin
  form-login + create-post). Лучший pick-rate, ловит ~50% дополнительных cred
  (по prod-статистике).
- `xmlrpc_only` — только XML-RPC. Самый дешёвый, но теряет XML-RPC-disabled
  сайты. Дефолт для совместимости со старыми runs.
- `admin_only` — сразу Tier 2. Дороже (3-4 HTTP-запроса вместо 1-2), нужно для
  сайтов где XML-RPC точно выключен (или Stage 3 link-placement задачи).

Revision ID: 0021_posting_method
Revises: 0020_capability_matrix
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_posting_method"
down_revision: str | Sequence[str] | None = "0020_capability_matrix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posting_runs",
        sa.Column(
            "posting_method",
            sa.String(20),
            nullable=False,
            server_default="auto",
        ),
    )


def downgrade() -> None:
    op.drop_column("posting_runs", "posting_method")
