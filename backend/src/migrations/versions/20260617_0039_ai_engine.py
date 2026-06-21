"""C2: AI-инфра — провайдеры / модели / шаблоны промптов.

ai_providers     — ключ доступа + тип (openai|anthropic|google) + base_url
ai_models        — конфиг модели (model_id, temperature, max_tokens, purpose)
prompt_templates — шаблоны генерации с {переменными} (как prompt_gen_content)

Ключ провайдера шифруется (core.crypto), как пароли cred/proxy.

Revision ID: 0039_ai_engine
Revises: 0038_content_engine
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_ai_engine"
down_revision = "0038_content_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),  # openai|anthropic|google
        sa.Column("api_key_enc", sa.Text(), nullable=False),       # Fernet-шифр
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", name="uq_ai_provider_name"),
    )
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="4096"),
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="content"),  # content|spin|any
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["ai_providers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_ai_models_provider", "ai_models", ["provider_id"])
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", name="uq_prompt_template_name"),
    )


def downgrade() -> None:
    op.drop_table("prompt_templates")
    op.drop_index("ix_ai_models_provider", table_name="ai_models")
    op.drop_table("ai_models")
    op.drop_table("ai_providers")
