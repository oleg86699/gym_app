"""
Хеширование паролей (bcrypt) и JWT (PyJWT).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from core.config import settings

# bcrypt: пароли длиннее 72 байт обрезаются, это документированное поведение.
# В реальной жизни никто не использует пароли > 72 байт; UI ограничивает 200,
# тут на всякий случай тоже truncate.
_BCRYPT_MAX_BYTES = 72


# ─── Passwords ────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Хешировать пароль bcrypt-ом. Возвращает строку utf-8."""
    if not plain:
        raise ValueError("password must be non-empty")
    pwd_bytes = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Сверить открытый пароль с хешем. False при пустых входах или ошибке."""
    if not plain or not hashed:
        return False
    try:
        pwd_bytes = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pwd_bytes, hashed.encode("utf-8"))
    except Exception:
        return False


# ─── JWT ──────────────────────────────────────────────────────────────


def create_access_token(
    subject: str,
    extra: dict[str, Any] | None = None,
    ttl_hours: int | None = None,
) -> str:
    """Создать JWT с `sub=subject`. extra — доп. claim-ы (например, user_id)."""
    now = datetime.now(UTC)
    ttl = ttl_hours if ttl_hours is not None else settings.JWT_TTL_HOURS

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl)).timestamp()),
    }
    if extra:
        payload.update(extra)

    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Раскодировать JWT. None если просрочен / битый / неверный signature."""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
    except jwt.PyJWTError:
        return None
