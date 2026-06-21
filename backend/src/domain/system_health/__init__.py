"""Infra/operational health — для health-dashboard.

Собирает «здоровье» инфраструктуры (очереди, прокси, FS, БД-пул, активные
runs/batches, недавние ошибки). Каждый probe best-effort: падение одного
не ломает весь дашборд.
"""

from .service import gather_system_health

__all__ = ["gather_system_health"]
