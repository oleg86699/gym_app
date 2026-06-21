"""
Celery воркеры — для тяжёлых long-running задач постинга.

Запуск (внутри docker-compose контейнера celery-worker):
    celery -A core.celery_app:celery_app worker --loglevel=INFO
"""
