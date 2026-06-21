"""Re-export моделей. Alembic-у этого достаточно чтобы увидеть metadata."""

from infrastructure.db.models.admin import (  # noqa: F401
    AdminGroup,
    AdminPage,
    AdminPermission,
    AdminRole,
    AdminUser,
    role_pages,
    role_permissions,
    user_pages,
    user_roles,
)
from infrastructure.db.models.app_settings import AppSettings  # noqa: F401
from infrastructure.db.models.audit import AuditLog  # noqa: F401
from infrastructure.db.models.invitation import Invitation  # noqa: F401
from infrastructure.db.models.posting import (  # noqa: F401
    CELERY_PRIORITY_MAP,
    PostingRun,
    PostingRunPriority,
    PostingRunStatus,
    ProjectWpUsed,
    RunArtifact,
    RunArtifactKind,
    RunTaskType,
    TextItem,
    TextItemStatus,
)
from infrastructure.db.models.project import (  # noqa: F401
    Project,
    ProjectDomain,
    group_projects,
    user_projects,
)
from infrastructure.db.models.proxy import Proxy  # noqa: F401
from infrastructure.db.models.texts import Text  # noqa: F401
from infrastructure.db.models.ai import (  # noqa: F401
    AiModel,
    AiProvider,
    PromptTemplate,
)
from infrastructure.db.models.site_event import SiteEvent  # noqa: F401
from infrastructure.db.models.wp_access import WpCredential, WpSite  # noqa: F401
from infrastructure.db.models.wp_batch import WpBatchStatus, WpImportBatch  # noqa: F401
