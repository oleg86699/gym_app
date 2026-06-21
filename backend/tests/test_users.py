"""Users CRUD smoke."""

from __future__ import annotations

from httpx import AsyncClient


async def test_list_users(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    res = await client.get("/admin/api/users", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    # как минимум сам super_admin
    assert any(u["is_active"] for u in data["items"])


async def test_create_and_delete_user(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    import secrets

    username = f"testuser_{secrets.token_hex(4)}"

    # create
    res = await client.post(
        "/admin/api/users",
        headers=auth_headers,
        json={"username": username, "password": "test_password_123"},
    )
    assert res.status_code == 201, res.text
    created = res.json()
    user_id = created["id"]
    assert created["username"] == username

    # list contains it
    res2 = await client.get(f"/admin/api/users?search={username}", headers=auth_headers)
    assert res2.status_code == 200
    assert any(u["id"] == user_id for u in res2.json()["items"])

    # delete
    res3 = await client.delete(f"/admin/api/users/{user_id}", headers=auth_headers)
    assert res3.status_code == 204

    # gone
    res4 = await client.get(f"/admin/api/users/{user_id}", headers=auth_headers)
    assert res4.status_code == 404


async def test_pages_me(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    res = await client.get("/admin/api/pages/me", headers=auth_headers)
    assert res.status_code == 200
    pages = res.json()
    # super_admin видит все активные страницы
    assert "/dashboard" in pages
    assert "/users" in pages
