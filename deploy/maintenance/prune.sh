#!/bin/bash
# Чистка docker-объектов (см. ADR-013).
# Запускается из cron в backup-контейнере раз в неделю + вручную через `make prune`.
#
# НИКОГДА не делаем `docker volume prune` — bind mounts защищены, но привычка важна.

set -euo pipefail

TIMESTAMP="$(date +%Y-%m-%d_%H%M)"
LOG_PREFIX="[prune ${TIMESTAMP}]"

# Защита: критичные хостовые папки должны быть смонтированы
for required in /var/lib/postgresql/data; do
    if [ ! -d "$required" ]; then
        echo "${LOG_PREFIX} FATAL: $required missing — aborting prune" >&2
        exit 1
    fi
done

echo "${LOG_PREFIX} START"

# Удалить dangling и unused images старше 7 дней (168h)
echo "${LOG_PREFIX} docker image prune"
docker image prune -af --filter 'until=168h' 2>&1 | tail -3 || true

# Builder cache старше 7 дней
echo "${LOG_PREFIX} docker builder prune"
docker builder prune -af --filter 'until=168h' 2>&1 | tail -3 || true

# Остановленные контейнеры старше 7 дней
echo "${LOG_PREFIX} docker container prune"
docker container prune -f --filter 'until=168h' 2>&1 | tail -3 || true

# Неиспользуемые networks старше 7 дней
echo "${LOG_PREFIX} docker network prune"
docker network prune -f --filter 'until=168h' 2>&1 | tail -3 || true

# Volumes — намеренно НЕ трогаем (см. ADR-014)
echo "${LOG_PREFIX} (skipping volume prune by design — see ADR-014)"

# Disk usage отчёт
echo "${LOG_PREFIX} disk usage:"
docker system df 2>&1 | sed "s/^/${LOG_PREFIX}   /"

echo "${LOG_PREFIX} OK"
