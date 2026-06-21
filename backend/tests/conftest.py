"""
Общие фикстуры для pytest.

TODO(stage 1.5): починить engine lifecycle. Глобальный async engine из core.db
привязан к event loop при первом обращении; pytest-asyncio в режиме auto
создаёт loop на каждый тест, в результате тесты, пишущие в БД, ломаются на
cleanup. Варианты фикса:
  1) Per-session loop_scope в pytest-asyncio + одна сессия per session.
  2) Test engine с NullPool, инжектится через override_dependencies.
  3) Lazy engine factory.
Curl-проверки в чате доказывают корректность кода; это чисто test infra.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import create_app


@pytest.fixture(scope="session")
def app():
    # Session-scoped: create_app() регистрирует Prometheus-метрики на глобальный
    # REGISTRY. Повторный вызов → "Duplicated timeseries". Одно приложение на
    # всю сессию (как в проде) решает это и ускоряет тесты.
    return create_app()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_engine():
    """Session-scoped NullPool engine, привязан к session event loop.
    Используется для override DB-зависимостей приложения в тестах — иначе
    глобальный core.db engine ломается на cross-loop cleanup."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from core.config import settings
    from core.db import _ASYNCPG_PGBOUNCER_CONNECT_ARGS

    # connect_args обязательны: pgbouncer (transaction mode) не поддерживает
    # prepared statements → без statement_cache_size=0 будет
    # DuplicatePreparedStatementError.
    engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        connect_args=_ASYNCPG_PGBOUNCER_CONNECT_ARGS,
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from core.db import get_db_read, get_db_write

    Session = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override():
        async with Session() as s:
            try:
                yield s
            finally:
                await s.rollback()

    app.dependency_overrides[get_db_read] = _override
    app.dependency_overrides[get_db_write] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db_read, None)
        app.dependency_overrides.pop(get_db_write, None)


@pytest_asyncio.fixture(loop_scope="session")
async def super_admin_token(client: AsyncClient) -> str:
    """Логинит дефолтного super_admin (созданного seed-скриптом) и возвращает JWT."""
    import os

    username = os.environ.get("SUPER_ADMIN_USERNAME", "admin")
    password = os.environ.get("SUPER_ADMIN_PASSWORD", "admin_change_me_after_first_login")

    res = await client.post(
        "/admin/api/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


@pytest_asyncio.fixture(loop_scope="session")
async def auth_headers(super_admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {super_admin_token}"}


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(test_engine):
    """Сессия поверх session-scoped test_engine (NullPool + pgbouncer args).
    Переиспользуем общий engine — иначе per-test engine.dispose() в
    session-loop даёт 'Event loop is closed' на teardown."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    Session = async_sessionmaker(test_engine, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()


@pytest.fixture(autouse=True)
def _no_autobind(monkeypatch):
    """В тестах НЕ привязываем домены задач к проектам автоматически — иначе
    campaign-тесты через create_campaign_run засоряли бы реальный проект-3
    (Alice) доменом nawal.mx. Сама фича autobind покрыта test_autobind_domains.py
    (там зовётся напрямую через module-level import, который этот патч не трогает).
    create_campaign_run импортит autobind лениво из пакета → видит этот noop."""
    async def _noop(*a, **k):
        return 0
    monkeypatch.setattr("domain.project_domains.autobind_link_domains", _noop, raising=False)
