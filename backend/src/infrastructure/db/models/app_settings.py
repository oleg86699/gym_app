"""
Глобальные настройки приложения — singleton-таблица (id=1).

Редактируются только super_admin-ом через /admin/api/app-settings.
Используются как дефолты для PostingRun и потолки для UI.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Integer
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.db.base import Base, TimestampedMixin


class AppSettings(Base, TimestampedMixin):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Постинг: единый дефолтный concurrency и таймаут на все прогоны.
    # Менеджеры не задают это per-run, чтобы не положить сервис кучей семафоров.
    default_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="25"
    )
    default_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )

    # Глобальный потолок одновременных постов через ВСЕ run-ы и celery-процессы
    # (жёсткий лимитер дефицитного ресурса: пул прокси / PgBouncer / трафик).
    # Делится между активными run-ами → «всё двигается понемногу».
    global_posting_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="80"
    )
    # Content Engine: потолок переиспользований спина одного оригинала (reuse).
    max_spin_reuse: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="50"
    )

    # CF Tier 3: сколько браузер-контекстов (Patchright) гоняем одновременно при
    # обходе Cloudflare. Браузер ~150-400МБ — крутить по RAM сервера. Только
    # медленная CF-полоса (~8% сайтов); тюнится под железо без рестарта.
    cf_browser_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )

    # Fair-share: минимальная конкурентность одного прогона при дележе
    # global_posting_concurrency между многими активными прогонами. Реальная
    # конкурентность прогона = clamp(global // активные_прогоны, floor,
    # run.concurrency). Одинокий прогон забивает весь сервер, при многих —
    # делится честно, но никто не опускается ниже floor.
    posting_concurrency_floor: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    # Сколько site-class фейлов подряд → выключить сайт безусловно (даже с
    # valid-cred). Счётчик сбрасывается в 0 при любом успехе.
    site_disable_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="25"
    )
    # Отдельный агрессивный порог для CF-challenge (сайт под Cloudflare редко
    # «оживает», а headful-фейл дорогой ~30 сек).
    site_disable_threshold_cf: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="8"
    )

    # Окно публикации: каждому посту воркер ставит случайную дату в диапазоне
    # [publish_from, publish_to]. Если оба NULL — все посты публикуются текущим
    # моментом. Если окно уже в прошлом — воркер клампит к now (см. posting.py).
    default_publish_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    default_publish_to: Mapped[date | None] = mapped_column(Date, nullable=True)
