"""admin_users.login_token_enc — retrievable supplier magic-link token

Раньше токен magic-ссылки хранился только хешем (login_token_hash) → ссылка
показывалась один раз при создании и терялась. Теперь дополнительно храним токен
ОБРАТИМО зашифрованным (как temp_password_enc для пароля) — super_admin может
скопировать/обновить ссылку в списке «Доступы поставщиков». Только для временных
supplier-аккаунтов; для обычных юзеров NULL.
"""
from alembic import op
import sqlalchemy as sa

# NB: alembic_version.version_num — varchar(32). Ревизия ≤32 символов!
revision = "0061_supplier_link_enc"
down_revision = "0060_content_gen_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("login_token_enc", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("admin_users", "login_token_enc")
