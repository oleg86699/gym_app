"""Tier 2: работа с wp-admin через httpx (без браузера).

Для случаев когда XML-RPC выключен/блокирован, но cred рабочая через
обычный wp-admin login. Также собирает capability-флаги: что именно
эта cred может на этом сайте (edit_pages / theme-editor / widgets).

См. `client.WpAdminClient` — основной API.
"""

from .client import (
    AdminCapabilities,
    AdminLoginKind,
    AdminPostKind,
    AdminUserCreateKind,
    LinkPlaceKind,
    LinkPlacementProbe,
    LinkPlaceOutcome,
    LoginOutcome,
    PostViaAdminOutcome,
    UserCreateOutcome,
    WpAdminClient,
    post_via_admin,
)

__all__ = [
    "AdminCapabilities",
    "AdminLoginKind",
    "AdminPostKind",
    "AdminUserCreateKind",
    "LinkPlaceKind",
    "LinkPlacementProbe",
    "LinkPlaceOutcome",
    "LoginOutcome",
    "PostViaAdminOutcome",
    "UserCreateOutcome",
    "WpAdminClient",
    "post_via_admin",
]
