"""/admin/api/queue — единая Global Queue (вся активная работа в одном месте).

Снапшот: posting-run-ы + батч валидации + статус глобального постинг-лимитера
(throttled индикатор). Доступно всем авторизованным (read-only обзор).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.middleware.auth import get_current_user
from core.db import get_db_read
from domain.queue.service import get_queue_snapshot
from infrastructure.db.models import AdminUser

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("")
async def get_queue(
    _: AdminUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_read),
) -> dict:
    return await get_queue_snapshot(session)
