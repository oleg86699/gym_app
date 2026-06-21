"""Health endpoints (без авторизации). Используются healthcheck-ом docker-а."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Простой liveness — отвечаем 200 если процесс живой."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe — проверяет БД и MinIO")
async def ready() -> JSONResponse:
    """Readiness — готов ли принимать трафик."""
    from core.db import ping
    from core.storage import storage

    db_ok = await ping()
    minio_ok = storage.ping()

    if not (db_ok and minio_ok):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "db": "up" if db_ok else "down",
                "minio": "up" if minio_ok else "down",
            },
        )
    return JSONResponse(content={"status": "ready", "db": "up", "minio": "up"})
