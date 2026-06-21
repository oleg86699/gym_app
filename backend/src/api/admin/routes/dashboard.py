"""/admin/api/dashboard — сводка для главной."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user
from api.admin.schemas.dashboard import DashboardResponse
from core.db import get_db_read
from domain.dashboard.service import get_dashboard
from domain.system_health import gather_system_health
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
async def get_dashboard_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> DashboardResponse:
    data = await get_dashboard(session, viewer)
    return DashboardResponse.model_validate(data)


@router.get("/system-health")
async def system_health_endpoint(
    viewer: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> dict:
    """Инфра-здоровье: очереди, прокси, CF-браузер (Patchright), БД-пул, активные
    runs/batches, недавние ошибки. Для health-dashboard. Best-effort."""
    return await gather_system_health(session)
