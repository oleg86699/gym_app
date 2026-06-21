"""
FastAPI application factory.

Импортируется uvicorn-ом как `main:create_app --factory`.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.admin.router import admin_router
from api.public.health import router as health_router
from api.public.invitations import router as public_invitations_router
from api.public.portal import router as public_portal_router
from core.config import settings
from core.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Создать и сконфигурировать FastAPI-приложение."""
    configure_logging(level=settings.LOG_LEVEL, json=settings.ENVIRONMENT != "dev")

    app = FastAPI(
        title="gym_app API",
        version="0.0.1",
        description="Альтернатива Zebroid: массовый постинг на WordPress через XML-RPC.",
        docs_url="/admin/api/docs" if settings.ENVIRONMENT == "dev" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.ENVIRONMENT == "dev" else None,
    )

    # ─── Middleware: trace_id ─────────────────────────────────────────
    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Trace-Id"] = trace_id
        return response

    # ─── CORS ─────────────────────────────────────────────────────────
    if settings.allowed_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ─── Startup: orphan-batch recovery ──────────────────────────────
    # При рестарте app любые batches застрявшие в status='validating' (worker
    # упал, контейнер перезапустили mid-flight) останутся VALIDATING навсегда
    # и UI будет крутить spinner. Здесь — мягко чиним: если последний
    # cred-validation был > 5 минут назад, считаем что валидация умерла,
    # помечаем done с finished_at = последнее cred-обновление.
    @app.on_event("startup")
    async def _heal_orphan_batches() -> None:
        from datetime import datetime, timedelta, UTC
        from sqlalchemy import select, update, func
        from core.db import WriteSession
        from infrastructure.db.models.wp_batch import WpImportBatch, WpBatchStatus
        from infrastructure.db.models.wp_access import WpCredential

        try:
            async with WriteSession() as s:
                stuck = (await s.execute(
                    select(WpImportBatch).where(
                        WpImportBatch.status == WpBatchStatus.VALIDATING.value,
                    )
                )).scalars().all()
                threshold = datetime.now(UTC) - timedelta(minutes=5)
                healed = 0
                for b in stuck:
                    last_activity = await s.scalar(
                        select(func.max(WpCredential.last_validated_at))
                        .where(WpCredential.import_batch_id == b.id)
                    )
                    if last_activity and last_activity >= threshold:
                        # Активная валидация (cred обновлялся недавно) — оставляем
                        continue
                    finished_at = last_activity or b.validation_started_at or datetime.now(UTC)
                    await s.execute(
                        update(WpImportBatch).where(WpImportBatch.id == b.id).values(
                            status=WpBatchStatus.DONE.value,
                            validation_finished_at=finished_at,
                            pause_requested=False,
                        )
                    )
                    healed += 1
                    logger.warning("startup.batch_healed", extra={
                        "batch_id": b.id, "finished_at": str(finished_at),
                    })
                if healed:
                    await s.commit()
                    logger.info("startup.orphan_batches_recovered", extra={"count": healed})
        except Exception as e:
            logger.error("startup.heal_failed", extra={"error": str(e)})

    # ─── Routes ──────────────────────────────────────────────────────
    # /health, /ready — для docker healthcheck (идёт напрямую в контейнер)
    app.include_router(health_router, tags=["health"])
    # /admin/api/system/health — для UI через nginx
    app.include_router(health_router, prefix="/admin/api/system", tags=["health"])
    # Public invite endpoints (без авторизации)
    app.include_router(public_invitations_router)
    # Public magic-login поставщика
    app.include_router(public_portal_router)
    # Админ-API
    app.include_router(admin_router)

    # Prometheus metrics — /admin/api/system/metrics (исключён из OpenAPI)
    from core.metrics import setup_instrumentator
    setup_instrumentator(app)

    logger.info(
        "app.started",
        extra={"environment": settings.ENVIRONMENT, "log_level": settings.LOG_LEVEL},
    )
    return app
