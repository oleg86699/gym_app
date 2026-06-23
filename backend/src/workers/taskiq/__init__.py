"""
TaskIQ воркеры — лёгкие async-задачи.

Запуск:
    taskiq worker workers.taskiq:broker --workers 2
    taskiq scheduler workers.taskiq:scheduler

`scheduler` здесь — это TaskiqScheduler объект из core.taskiq_app
(периодические задачи). Сами задачи живут в модуле `cron_tasks` (имя
файла НЕ `scheduler.py` чтобы не было коллизии с экспортируемым именем).
"""

# Re-export брокера и cron-scheduler-а для taskiq CLI
from core.taskiq_app import broker, scheduler  # noqa: F401

# Импортируем модули с тасками, чтобы декораторы `@broker.task` выполнились
# и таски зарегистрировались.
from workers.taskiq import (  # noqa: F401
    campaign,
    cron_tasks,
    csv_direct,
    health,
    unpack,
    validate_links,
)
