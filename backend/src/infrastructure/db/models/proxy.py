"""
Proxy pool. Используется воркером постинга через `posting_runs.proxy_id`.

Pricing: воркер просто оборачивает httpx.AsyncClient в proxy URL —
`http://user:pass@host:port`. Никакой сложной браузерной интеграции (как
в langgraph_ai_browser) тут не нужно, основной use case — обход блокировок
со стороны WP/CDN.

Password хранится зашифрованным (Fernet) — см. core/crypto.py.
Метаданные обогащаются через `check` (ipify + ip-api lookup).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base, TimestampedMixin


class Proxy(Base, TimestampedMixin):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Endpoint
    protocol: Mapped[str] = mapped_column(String(10), nullable=False, default="http")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # encrypted via core/crypto.encrypt_password
    password: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Декларативные поля (из источника)
    country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # residential / mobile / datacenter / proxy / unknown
    proxy_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Статус + проверка
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 'active' / 'down' / 'unknown' — последний результат health-check-а
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Обогащённые данные после check
    external_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    isp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    asn: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Provenance: 'manual' / 'bulk' / '<provider_name>' (soax, decodo, webshare).
    # (source, source_id) — upsert key при ре-импорте из провайдера.
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Health auto-disable (см. миграцию 0024): consecutive_failures накапливается
    # на каждой сетевой ошибке через прокси; при достижении threshold (5) прокси
    # «лочится» (locked_until = now + 30 мин). После окончания cooldown снова
    # доступен в pool. Если живой проксе подняли — counter сбрасывается.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
