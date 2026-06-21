"""
Симметричное шифрование секретов (Fernet).

Используется для wp_credentials.password — храним зашифрованным, дешифруем
только в воркере перед XML-RPC POST.

Ключ берётся из env `WP_CRED_ENC_KEY` (urlsafe base64, 32 байта).
В prod (ENVIRONMENT=prod) пустой ключ — fatal на старте.
В dev — fallback на детерминистический dev-ключ с громким warning, чтобы
разработчик не сидел без шифрования из-за забытой переменной.

⚠ Потеря ключа = потеря всех паролей. Бекапить ключ вместе с БД.
"""

from __future__ import annotations

import base64
import hashlib

import structlog
from cryptography.fernet import Fernet, InvalidToken

from core.config import settings

log = structlog.get_logger(__name__)

# Префикс зашифрованных токенов — позволяет миграции и коду различать
# plaintext (legacy) и Fernet-token. Чистый Fernet-token начинается с
# `gAAAAA...` (base64 от version byte 0x80 + timestamp). Префикс `enc:v1:`
# — дополнительная страховка и явный маркер версии формата.
ENC_PREFIX = "enc:v1:"

# Dev-fallback ключ. Заведомо известен, для prod НЕ использовать.
_DEV_FALLBACK_KEY_MATERIAL = "gym-app-dev-fallback-do-not-use-in-prod"

_fernet: Fernet | None = None


def _derive_dev_key() -> bytes:
    """Дет. dev-ключ — sha256 от константы, потом urlsafe-b64. Для dev only."""
    digest = hashlib.sha256(_DEV_FALLBACK_KEY_MATERIAL.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _load_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = settings.WP_CRED_ENC_KEY.strip()
    if not key:
        if settings.ENVIRONMENT == "prod":
            raise RuntimeError(
                "WP_CRED_ENC_KEY is required in production. Generate: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        log.warning(
            "crypto.dev_fallback_key_in_use",
            msg="WP_CRED_ENC_KEY не задан — используется dev-fallback. НЕ для prod.",
        )
        _fernet = Fernet(_derive_dev_key())
        return _fernet

    try:
        _fernet = Fernet(key.encode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"WP_CRED_ENC_KEY invalid (must be urlsafe-base64 32-byte key): {e}"
        ) from e
    return _fernet


# ─── Публичный API ───────────────────────────────────────────────────


def encrypt_password(plain: str) -> str:
    """plaintext → enc:v1:<fernet-token>. Безопасно вызывать на уже зашифрованных
    (вернёт как есть)."""
    if not plain:
        return plain
    if plain.startswith(ENC_PREFIX):
        return plain
    token = _load_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    return f"{ENC_PREFIX}{token}"


def decrypt_password(stored: str) -> str:
    """
    enc:v1:<token> → plaintext. Legacy plaintext (без префикса) возвращается
    как есть — для постепенной миграции данных без даунтайма.
    """
    if not stored:
        return stored
    if not stored.startswith(ENC_PREFIX):
        # Legacy plaintext; миграция 0014 такие зашифрует в одной транзакции.
        return stored
    token = stored[len(ENC_PREFIX):]
    try:
        return _load_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "WpCredential password decrypt failed — wrong WP_CRED_ENC_KEY?"
        ) from e


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(ENC_PREFIX)
