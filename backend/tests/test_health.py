"""Smoke-тест: /health отдаёт 200."""

from __future__ import annotations

from httpx import AsyncClient

# app/client фикстуры берутся из conftest.py (session-scoped app — иначе
# повторный create_app() даёт "Duplicated timeseries" на Prometheus).


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    # /ready отдаёт {status, db, minio} — проверяем ключевой флаг.
    assert response.json()["status"] == "ready"
