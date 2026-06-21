# Backend (FastAPI + uvicorn) image.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Манифест + lock — кеш слоя при изменениях кода не сбрасывается.
# --frozen: ставим РОВНО версии из uv.lock (детерминизм). Без lock-а uv sync
# ре-резолвил бы fastapi до 0.138.0 и ломал prometheus-instrumentator (см. пин).
COPY backend/pyproject.toml backend/uv.lock /app/
RUN uv sync --frozen --no-dev

# CF Tier 3: stealth-браузер (Patchright) для обхода Cloudflare Bot-Fight, где
# HTTP-клиент режется. ~300-500МБ (chromium + системные либы). Отдельный слой —
# не пересобирается при правках кода. Воркеры валидации/постинга запускают его
# только на ~8% CF-сайтов; модуль cf_browser лениво импортит patchright.
# patchright (python-пакет) ставится через uv sync из lock-а — здесь только
# скачиваем сам браузер chromium.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.patchright
RUN patchright install --with-deps chromium

# Xvfb — виртуальный дисплей. CF Managed Challenge ("Just a moment…") headless-
# хром НЕ проходит (жёсткий 403); headful под Xvfb — проходит. Воркеры запускают
# chromium с headless=False под DISPLAY=:99 (см. command в compose). GPU не нужен.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

# Код приложения + alembic.ini + миграции
COPY backend/alembic.ini /app/alembic.ini
COPY backend/src/ /app/src/

EXPOSE 8080

# По умолчанию: накатить миграции → засидить → запустить uvicorn.
# Override в docker-compose.dev.yaml добавляет --reload.
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.seed && uvicorn main:create_app --factory --host 0.0.0.0 --port 8080"]
