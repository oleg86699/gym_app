"""tag-access RBAC: allowed_tags allowlist on admin_users + admin_groups.

Per-user / per-group allowlist of batch tags (WpImportBatch.tag). NULL = no
restriction at that level (all tags). Effective set for a non-super user =
intersection of the group's and the user's allowlists (NULL = "all" at that
level). super_admin is always unrestricted. This lets super_admin split the
credential pool across teams (group A → tags 1,2,3; group B → 4,5,6) and lets a
group_admin narrow individual members within their group's allowed tags.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0052_tag_access_allowlist"
down_revision = "0051_two_level_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for tbl in ("admin_users", "admin_groups"):
        op.add_column(
            tbl,
            sa.Column("allowed_tags", postgresql.ARRAY(sa.String(100)), nullable=True),
        )


def downgrade() -> None:
    for tbl in ("admin_users", "admin_groups"):
        op.drop_column(tbl, "allowed_tags")
