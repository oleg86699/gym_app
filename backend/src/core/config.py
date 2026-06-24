"""
Конфигурация приложения через pydantic-settings.

Все значения читаются из переменных окружения. Дефолты безопасные для dev,
для прода — обязательно переопределять через .env.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ─── Окружение ─────────────────────────────────────────────────
    ENVIRONMENT: str = Field(default="dev", description="dev | staging | prod")
    LOG_LEVEL: str = Field(default="INFO")
    TZ: str = Field(default="Europe/Kyiv")

    # ─── Web ───────────────────────────────────────────────────────
    APP_PORT: int = Field(default=8080)
    ALLOWED_ORIGINS: str = Field(default="", description="через запятую")
    # Базовый URL UI для построения публичных ссылок (invite, password-reset).
    # Если пусто — выводится из заголовков X-Forwarded-Host/Proto.
    # В dev/проде ставить явно (например http://localhost:28000 или https://app.example.com).
    PUBLIC_BASE_URL: str = Field(default="")

    # ─── Database ──────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://gym_app:local@pgbouncer:5432/gym_app",
        description="DSN для пишущих операций (через pgbouncer)",
    )
    DATABASE_READ_URL: str = Field(
        default="",
        description="DSN для read-only. Пусто → используется DATABASE_URL.",
    )

    # ─── Redis ─────────────────────────────────────────────────────
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # ─── Auth ──────────────────────────────────────────────────────
    JWT_SECRET: str = Field(default="change_me_dev_only")
    JWT_ALG: str = Field(default="HS256")
    JWT_TTL_HOURS: int = Field(default=24)

    # ─── Super admin (seed) ────────────────────────────────────────
    SUPER_ADMIN_USERNAME: str = Field(default="admin")
    SUPER_ADMIN_PASSWORD: str = Field(default="change_me")
    SUPER_ADMIN_EMAIL: str = Field(default="")

    # ─── Лимиты ────────────────────────────────────────────────────
    MAX_ACTIVE_RUNS_PER_USER: int = Field(default=5)

    # ─── Шифрование секретов ───────────────────────────────────────
    # Fernet key (urlsafe base64, 32 bytes). Если пусто — приложение откажется
    # стартовать на ENVIRONMENT=prod; в dev fallback на детерминистический ключ
    # с громким warning (см. core/crypto.py).
    # Сгенерировать: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    WP_CRED_ENC_KEY: str = Field(default="")

    # ─── Queues (Celery + TaskIQ) ──────────────────────────────────
    CELERY_BROKER_URL: str = Field(default="redis://redis:6379/1")
    CELERY_RESULT_BACKEND: str = Field(default="redis://redis:6379/2")
    CELERY_WORKER_CONCURRENCY: int = Field(default=2)
    TASKIQ_BROKER_URL: str = Field(default="redis://redis:6379/3")
    TASKIQ_RESULT_BACKEND: str = Field(default="redis://redis:6379/4")
    # Дефолтная concurrency валидации батча (если в диалоге/resume не задано явно).
    # Per-server: на мощном сервере подними в .env (напр. 30). 1 поток ≈ 1 кред параллельно.
    DEFAULT_VALIDATION_CONCURRENCY: int = Field(default=5, ge=1, le=50)

    # ─── MinIO ─────────────────────────────────────────────────────
    MINIO_ENDPOINT: str = Field(default="minio:9000")
    MINIO_ROOT_USER: str = Field(default="minioadmin")
    MINIO_ROOT_PASSWORD: str = Field(default="minioadmin_change_me")
    MINIO_USE_HTTPS: bool = Field(default=False)
    MINIO_REGION: str = Field(default="us-east-1")
    MINIO_BUCKET_TEXT_ITEMS: str = Field(default="text-items")
    MINIO_BUCKET_RESULTS: str = Field(default="results")
    MINIO_BUCKET_UPLOADS: str = Field(default="uploads-tmp")

    @property
    def effective_read_url(self) -> str:
        return self.DATABASE_READ_URL or self.DATABASE_URL

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()
