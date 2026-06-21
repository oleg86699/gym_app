"""Зашифровать существующие plaintext wp_credentials.password (Fernet).

Использует тот же WP_CRED_ENC_KEY из env, который читает рантайм. После
миграции все значения помечены префиксом `enc:v1:` (см. core/crypto.py).

Идемпотентно: уже зашифрованные строки (с префиксом) не трогает.

Revision ID: 0014_encrypt_wp_passwords
Revises: 0013_hot_autovacuum
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_encrypt_wp_passwords"
down_revision: str | Sequence[str] | None = "0013_hot_autovacuum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Импортируем тут чтобы alembic offline-mode (--sql) не падал на отсутствии
    # настроек/cryptography в момент генерации SQL.
    from core.crypto import ENC_PREFIX, encrypt_password

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, password FROM wp_credentials WHERE password IS NOT NULL")
    ).fetchall()

    encrypted = 0
    skipped = 0
    for row in rows:
        cred_id, password = row.id, row.password
        if not password or password.startswith(ENC_PREFIX):
            skipped += 1
            continue
        token = encrypt_password(password)
        bind.execute(
            sa.text("UPDATE wp_credentials SET password = :p WHERE id = :id"),
            {"p": token, "id": cred_id},
        )
        encrypted += 1

    print(f"[0014] wp_credentials encrypted: {encrypted}, already encrypted/skipped: {skipped}")


def downgrade() -> None:
    # Расшифровка возможна — но downgrade паролей в plaintext нежелателен.
    # Если очень нужно — отдельная скриптовая операция, не миграция.
    raise NotImplementedError(
        "Downgrade намеренно отключён: расшифровка паролей обратно в plaintext "
        "должна быть осознанной ручной операцией"
    )
