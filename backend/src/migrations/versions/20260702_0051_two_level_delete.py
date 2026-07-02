"""two-level delete: soft-delete for users, hard purge for super_admin.

- project_domains gets `deleted_at` (was hard-delete only) → soft-delete like
  projects/runs.
- projects / posting_runs / project_domains get `deleted_by` (which admin ran
  the soft-delete) so super_admin can audit who hid what.
- the (project_id, domain) uniqueness becomes PARTIAL (active rows only), so a
  soft-deleted money-domain can be re-added / restored without a constraint clash.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_two_level_delete"
down_revision = "0050_wp_site_posting_cooldown"
branch_labels = None
depends_on = None

_DELETED_BY_TABLES = ("projects", "posting_runs", "project_domains")


def upgrade() -> None:
    # soft-delete marker on money-domains
    op.add_column(
        "project_domains",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_project_domains_deleted_at", "project_domains", ["deleted_at"]
    )

    # who performed the soft-delete (audit for super_admin)
    for tbl in _DELETED_BY_TABLES:
        op.add_column(tbl, sa.Column("deleted_by", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{tbl}_deleted_by", tbl, "admin_users",
            ["deleted_by"], ["id"], ondelete="SET NULL",
        )

    # (project_id, domain) unique only among NON-deleted rows
    op.drop_constraint("uq_project_domain", "project_domains", type_="unique")
    op.create_index(
        "uq_project_domain_active", "project_domains", ["project_id", "domain"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_project_domain_active", table_name="project_domains")
    op.create_unique_constraint(
        "uq_project_domain", "project_domains", ["project_id", "domain"]
    )
    for tbl in _DELETED_BY_TABLES:
        op.drop_constraint(f"fk_{tbl}_deleted_by", tbl, type_="foreignkey")
        op.drop_column(tbl, "deleted_by")
    op.drop_index("ix_project_domains_deleted_at", table_name="project_domains")
    op.drop_column("project_domains", "deleted_at")
