"""require_page_access — backend-гейт страницы, единый источник истины с UI
(accessible_page_paths). super_admin и явно назначенная страница проходят,
остальные → 403. Бэкает ограничение /batches на super_admin-by-default."""
from __future__ import annotations

import pytest
from api.admin.middleware.auth import require_page_access
from fastapi import HTTPException


class _StubUser:
    def __init__(self, pages: set[str]):
        self._pages = pages

    def accessible_page_paths(self) -> set[str]:
        return self._pages


async def test_super_admin_passes():
    checker = require_page_access("/batches")
    # super_admin → accessible_page_paths() == {"*"}
    assert await checker(_StubUser({"*"})) is not None


async def test_explicit_grant_passes():
    checker = require_page_access("/batches")
    assert await checker(_StubUser({"/projects", "/batches"})) is not None


async def test_no_grant_forbidden():
    checker = require_page_access("/batches")
    with pytest.raises(HTTPException) as ei:
        await checker(_StubUser({"/projects", "/runs"}))
    assert ei.value.status_code == 403


async def test_other_page_grant_does_not_leak():
    checker = require_page_access("/batches")
    # доступ к /wp-sites не даёт /batches
    with pytest.raises(HTTPException) as ei:
        await checker(_StubUser({"/wp-sites"}))
    assert ei.value.status_code == 403
