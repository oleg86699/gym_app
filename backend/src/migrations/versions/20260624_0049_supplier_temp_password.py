"""admin_users.temp_password_enc — обратимо зашифрованный пароль временных
supplier-аккаунтов, чтобы super_admin мог посмотреть/скопировать его позже на
странице «Доступы поставщиков». Для обычных юзеров NULL (только hash)."""
from alembic import op
import sqlalchemy as sa

revision = "0049_supplier_temp_password"
down_revision = "0048_link_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column("temp_password_enc", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("admin_users", "temp_password_enc")
