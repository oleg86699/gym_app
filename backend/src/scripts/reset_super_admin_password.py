"""
Одноразовый сброс пароля super_admin из текущего SUPER_ADMIN_PASSWORD в .env.

Использование:
    # сначала отредактируй .env с новым паролем, потом:
    make down && make up                  # чтобы перечитать env
    docker compose exec app python -m scripts.reset_super_admin_password

Идемпотентен: повторный запуск с тем же паролем = тот же хеш не запишется,
но если перезапустить с другим — обновится.

Полезно когда:
- забыли пароль super_admin
- сменили SUPER_ADMIN_PASSWORD в .env и хотим, чтобы он применился
- надо принудительно сбросить пароль из-за инцидента
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import select, update

from core.config import settings
from core.db import WriteSession
from core.logging import configure_logging
from core.security import hash_password
from infrastructure.db.models import AdminUser

log = structlog.get_logger(__name__)


async def _reset() -> int:
    new_hash = hash_password(settings.SUPER_ADMIN_PASSWORD)
    async with WriteSession() as session:
        user = (
            await session.execute(
                select(AdminUser).where(AdminUser.username == settings.SUPER_ADMIN_USERNAME)
            )
        ).scalar_one_or_none()
        if user is None:
            log.error(
                "reset.super_admin.not_found",
                username=settings.SUPER_ADMIN_USERNAME,
                hint="Создай его сначала через `make seed` или меняй SUPER_ADMIN_USERNAME в .env",
            )
            return 1

        await session.execute(
            update(AdminUser)
            .where(AdminUser.id == user.id)
            .values(hashed_password=new_hash)
        )
        await session.commit()
        log.info("reset.super_admin.password_updated", user_id=user.id, username=user.username)
        return 0


def main() -> None:
    configure_logging(level=settings.LOG_LEVEL, json=settings.ENVIRONMENT != "dev")
    sys.exit(asyncio.run(_reset()))


if __name__ == "__main__":
    main()
