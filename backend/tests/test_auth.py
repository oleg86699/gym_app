"""Auth scenarios."""

from __future__ import annotations

import os

from httpx import AsyncClient


async def test_login_bad_credentials(client: AsyncClient) -> None:
    res = await client.post(
        "/admin/api/auth/login",
        json={"username": "nonexistent_user", "password": "wrong"},
    )
    assert res.status_code == 401


async def test_login_super_admin(client: AsyncClient) -> None:
    username = os.environ.get("SUPER_ADMIN_USERNAME", "admin")
    password = os.environ.get("SUPER_ADMIN_PASSWORD", "admin_change_me_after_first_login")

    res = await client.post(
        "/admin/api/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["user"]["is_super_admin"] is True
    assert "admin_token" in res.headers.get("set-cookie", "")


async def test_me_requires_auth(client: AsyncClient) -> None:
    res = await client.get("/admin/api/auth/me")
    assert res.status_code == 401


async def test_me_returns_user(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    res = await client.get("/admin/api/auth/me", headers=auth_headers)
    assert res.status_code == 200
    me = res.json()
    assert me["is_super_admin"] is True
    assert "*" in me["accessible_pages"]
