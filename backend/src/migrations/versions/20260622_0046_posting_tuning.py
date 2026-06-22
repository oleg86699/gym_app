"""app_settings: тюнинг постинга под сервер — floor конкурентности (fair-share)
и пороги авто-выключения мёртвых сайтов (общий / отдельный для CF). Всё
правится в /settings без рестарта."""
from alembic import op
import sqlalchemy as sa

revision = "0046_posting_tuning"
down_revision = "0045_user_temp_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Минимальная конкурентность одного прогона при дележе global-ёмкости между
    # многими активными прогонами (fair-share: никто не голодает).
    op.add_column("app_settings", sa.Column(
        "posting_concurrency_floor", sa.Integer(), nullable=False, server_default="5"))
    # Сколько site-class фейлов подряд → выключить сайт БЕЗУСЛОВНО (даже с
    # valid-cred). Закрывает дыру, из-за которой «протухшие» сайты жили вечно.
    op.add_column("app_settings", sa.Column(
        "site_disable_threshold", sa.Integer(), nullable=False, server_default="25"))
    # Отдельный (более агрессивный) порог для CF-challenge: сайт под Cloudflare
    # почти не «оживает» сам, а каждый headful-фейл стоит ~30 сек.
    op.add_column("app_settings", sa.Column(
        "site_disable_threshold_cf", sa.Integer(), nullable=False, server_default="8"))


def downgrade() -> None:
    op.drop_column("app_settings", "site_disable_threshold_cf")
    op.drop_column("app_settings", "site_disable_threshold")
    op.drop_column("app_settings", "posting_concurrency_floor")
