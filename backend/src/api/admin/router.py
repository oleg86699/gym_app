"""Композитный роутер админ-панели — всё под /admin/api/*."""

from __future__ import annotations

from fastapi import APIRouter

from api.admin.routes.ai_settings import router as ai_settings_router
from api.admin.routes.app_settings import router as app_settings_router
from api.admin.routes.audit import router as audit_router
from api.admin.routes.auth import router as auth_router
from api.admin.routes.dashboard import router as dashboard_router
from api.admin.routes.groups import router as groups_router
from api.admin.routes.invitations import router as invitations_router
from api.admin.routes.pages import router as pages_router
from api.admin.routes.postings import postings_router, project_postings_router
from api.admin.routes.proxies import router as proxies_router
from api.admin.routes.projects import router as projects_router
from api.admin.routes.queue import router as queue_router
from api.admin.routes.roles import permissions_router, roles_router
from api.admin.routes.supplier_access import router as supplier_access_router
from api.admin.routes.text_items import router as text_items_router
from api.admin.routes.texts import router as texts_router
from api.admin.routes.users import router as users_router
from api.admin.routes.wp_batches import router as wp_batches_router
from api.admin.routes.wp_sites import router as wp_sites_router

admin_router = APIRouter(prefix="/admin/api")

admin_router.include_router(auth_router)
admin_router.include_router(users_router)
admin_router.include_router(groups_router)
admin_router.include_router(roles_router)
admin_router.include_router(permissions_router)
admin_router.include_router(pages_router)
admin_router.include_router(projects_router)
admin_router.include_router(project_postings_router)
admin_router.include_router(postings_router)
admin_router.include_router(text_items_router)
admin_router.include_router(texts_router)
admin_router.include_router(invitations_router)
admin_router.include_router(supplier_access_router)
admin_router.include_router(wp_sites_router)
admin_router.include_router(wp_batches_router)
admin_router.include_router(app_settings_router)
admin_router.include_router(dashboard_router)
admin_router.include_router(proxies_router)
admin_router.include_router(queue_router)
admin_router.include_router(audit_router)
admin_router.include_router(ai_settings_router)
