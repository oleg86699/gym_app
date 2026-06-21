#!/usr/bin/env bash
# Создаёт хостовые папки для persistent-данных (см. ADR-014).
# Запускается один раз перед первым `make up`.
# Идемпотентен: повторный запуск ничего не ломает.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# UID/GID для postgres-alpine = 70:70
# UID/GID для redis = 999:1000
# UID/GID для minio = 1000:1000 (этап 2)
# В bind-mount-ах docker мапит наружу, но владелец на хосте важен.

DIRS=(
    "data/postgres"
    "data/redis"
    "data/minio"           # пригодится в этапе 2
    "backups/postgres"
    "backups/minio"        # пригодится в этапе 2
    "backups/log"
    "logs"
)

echo "[init-host] creating persistent directories in $PROJECT_ROOT"
for d in "${DIRS[@]}"; do
    if [[ -d "$d" ]]; then
        echo "  exists: $d"
    else
        mkdir -p "$d"
        echo "  created: $d"
    fi
done

# На macOS Docker Desktop ownership проксируется через grpcfuse — chown не нужен.
# На Linux раскомментируй блок ниже, если postgres не сможет писать.
#
# if [[ "$(uname -s)" == "Linux" ]]; then
#     chown -R 70:70 data/postgres   2>/dev/null || sudo chown -R 70:70 data/postgres
#     chown -R 999:1000 data/redis   2>/dev/null || sudo chown -R 999:1000 data/redis
#     chown -R 1000:1000 data/minio  2>/dev/null || sudo chown -R 1000:1000 data/minio
# fi

echo "[init-host] OK"
echo ""
echo "Persistent data layout (см. deploy/PERSISTENT_PATHS.md):"
echo "  ./data/postgres   ← Postgres data dir"
echo "  ./data/redis      ← Redis AOF"
echo "  ./data/minio      ← MinIO objects (этап 2)"
echo "  ./backups/        ← pg_dump'ы и MinIO mirror"
echo ""
echo "Эти папки в .gitignore. НИКОГДА не удаляй их через docker volume prune."
