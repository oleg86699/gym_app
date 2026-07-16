"""app_settings.content_gen_concurrency — parallel csv_campaign generation

Рантайм-настройка скорости генерации: сколько текстов csv_campaign генерится
одновременно (bulk «Сгенерировать» / авто-старт постинга). Раньше генерация шла
строго последовательно (один await generate_item на айтем) — теперь bounded-
параллельно, значение тюнится без рестарта из настроек. AI-bound, упирается в
rate-limit ключа.
"""
from alembic import op
import sqlalchemy as sa

# NB: alembic_version.version_num — varchar(32). Ревизия ≤32 символов!
# "0060_content_gen_concurrency" = 28 — влезает.
revision = "0060_content_gen_concurrency"
down_revision = "0059_ai_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("content_gen_concurrency", sa.Integer(),
                  nullable=False, server_default="5"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "content_gen_concurrency")
