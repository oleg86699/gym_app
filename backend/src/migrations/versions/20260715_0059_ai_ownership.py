"""AI ownership + sharing — owner/group/shared_all + share pivots

Провайдеры (ключи) и промпты становятся владеемыми и шарящимися (как проекты):
owner_user_id + owner_group_id + shared_all, плюс pivot-таблицы шаринга на
пользователей/группы. Уникальность имени — в рамках владельца.

Бэкфилл: существующие глобальные ключи/промпты → первый super_admin + shared_all
(чтобы остались видны всем, как раньше).
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_ai_ownership"
down_revision = "0058_batch_val_concurrency"
branch_labels = None
depends_on = None


def _add_ownership(table: str, uq_old: str, uq_new: str) -> None:
    op.add_column(table, sa.Column(
        "owner_user_id", sa.Integer(),
        sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True))
    op.add_column(table, sa.Column(
        "owner_group_id", sa.Integer(),
        sa.ForeignKey("admin_groups.id", ondelete="SET NULL"), nullable=True))
    op.add_column(table, sa.Column(
        "shared_all", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])
    op.create_index(f"ix_{table}_owner_group_id", table, ["owner_group_id"])
    # уникальность имени: глобальная → в рамках владельца
    op.drop_constraint(uq_old, table, type_="unique")
    op.create_unique_constraint(uq_new, table, ["owner_user_id", "name"])


def _share_pivot(name: str, parent_table: str, parent_col: str, target: str) -> None:
    """target: 'user' → admin_user_id col; 'group' → group_id col."""
    if target == "user":
        second = sa.Column("admin_user_id", sa.Integer(),
                           sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True)
    else:
        second = sa.Column("group_id", sa.Integer(),
                           sa.ForeignKey("admin_groups.id", ondelete="CASCADE"), primary_key=True)
    op.create_table(
        name,
        sa.Column(parent_col, sa.Integer(),
                  sa.ForeignKey(f"{parent_table}.id", ondelete="CASCADE"), primary_key=True),
        second,
    )


# первый активный super_admin (владелец легаси-строк)
_FIRST_SUPER = (
    "SELECT u.id FROM admin_users u "
    "JOIN user_roles ur ON ur.admin_user_id = u.id "
    "JOIN admin_roles r ON r.id = ur.role_id "
    "WHERE r.name = 'super_admin' AND u.is_active = true "
    "ORDER BY u.id LIMIT 1"
)


def upgrade() -> None:
    _add_ownership("ai_providers", "uq_ai_provider_name", "uq_ai_provider_owner_name")
    _add_ownership("prompt_templates", "uq_prompt_template_name", "uq_prompt_template_owner_name")

    _share_pivot("ai_provider_users", "ai_providers", "provider_id", "user")
    _share_pivot("ai_provider_groups", "ai_providers", "provider_id", "group")
    _share_pivot("prompt_template_users", "prompt_templates", "prompt_id", "user")
    _share_pivot("prompt_template_groups", "prompt_templates", "prompt_id", "group")

    # Бэкфилл: легаси глобальные строки → первый super_admin, видны всем.
    for tbl in ("ai_providers", "prompt_templates"):
        op.execute(
            f"UPDATE {tbl} SET owner_user_id = ({_FIRST_SUPER}), shared_all = true "
            f"WHERE owner_user_id IS NULL"
        )
        # денормализованный кэш группы владельца
        op.execute(
            f"UPDATE {tbl} SET owner_group_id = "
            f"(SELECT group_id FROM admin_users WHERE id = {tbl}.owner_user_id) "
            f"WHERE owner_user_id IS NOT NULL"
        )


def downgrade() -> None:
    op.drop_table("prompt_template_groups")
    op.drop_table("prompt_template_users")
    op.drop_table("ai_provider_groups")
    op.drop_table("ai_provider_users")
    for tbl, uq_old, uq_new in (
        ("ai_providers", "uq_ai_provider_name", "uq_ai_provider_owner_name"),
        ("prompt_templates", "uq_prompt_template_name", "uq_prompt_template_owner_name"),
    ):
        op.drop_constraint(uq_new, tbl, type_="unique")
        op.create_unique_constraint(uq_old, tbl, ["name"])
        op.drop_index(f"ix_{tbl}_owner_group_id", tbl)
        op.drop_index(f"ix_{tbl}_owner_user_id", tbl)
        op.drop_column(tbl, "shared_all")
        op.drop_column(tbl, "owner_group_id")
        op.drop_column(tbl, "owner_user_id")
