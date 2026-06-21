"""admin_users.expires_at + is_temporary — временный доступ (поставщик доступов):
после expires_at вход/токен невалидны. is_temporary — маркер таких аккаунтов."""
from alembic import op
import sqlalchemy as sa

revision = "0045_user_temp_access"
down_revision = "0044_cf_browser_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column(
        "expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("admin_users", sa.Column(
        "is_temporary", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("admin_users", sa.Column(
        "login_token_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_admin_users_expires_at", "admin_users", ["expires_at"])
    op.create_index("ix_admin_users_login_token_hash", "admin_users", ["login_token_hash"])


def downgrade() -> None:
    op.drop_index("ix_admin_users_login_token_hash", table_name="admin_users")
    op.drop_index("ix_admin_users_expires_at", table_name="admin_users")
    op.drop_column("admin_users", "login_token_hash")
    op.drop_column("admin_users", "is_temporary")
    op.drop_column("admin_users", "expires_at")
