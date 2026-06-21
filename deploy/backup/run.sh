#!/bin/bash
# Запускается из cron внутри backup-контейнера.
# Делает: pg_dump → /backups/postgres/, mc mirror MinIO (этап 2) → /backups/minio/,
# ротация старше RETENTION_DAYS.

set -euo pipefail

TIMESTAMP="$(date +%Y-%m-%d_%H%M)"
LOG_PREFIX="[backup ${TIMESTAMP}]"

echo "${LOG_PREFIX} START"

# ─── Postgres ──────────────────────────────────────────────────────────
PG_DUMP_FILE="/backups/postgres/${TIMESTAMP}.dump"
PG_HOST="${POSTGRES_HOST:-db}"
echo "${LOG_PREFIX} pg_dump ${PG_HOST}/${POSTGRES_DB} -> ${PG_DUMP_FILE}"

PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
    --host="${PG_HOST}" \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    --format=custom \
    --compress=6 \
    --file="${PG_DUMP_FILE}"

PG_SIZE_HUMAN="$(du -h "${PG_DUMP_FILE}" | cut -f1)"
echo "${LOG_PREFIX} pg_dump OK (${PG_SIZE_HUMAN})"

# ─── MinIO (этап 2) ────────────────────────────────────────────────────
# Активируется когда minio будет в стеке.
if mc alias set minio "http://minio:9000" \
       "${MINIO_ROOT_USER:-}" "${MINIO_ROOT_PASSWORD:-}" >/dev/null 2>&1; then
    echo "${LOG_PREFIX} mc mirror minio buckets"
    for bucket in text-items results; do
        if mc ls "minio/${bucket}" >/dev/null 2>&1; then
            mc mirror --overwrite --remove \
                "minio/${bucket}" "/backups/minio/${bucket}/"
            echo "${LOG_PREFIX} mc mirror ${bucket} OK"
        fi
    done
else
    echo "${LOG_PREFIX} (minio not configured, skipping mirror)"
fi

# ─── Ротация ───────────────────────────────────────────────────────────
RETENTION="${RETENTION_DAYS:-7}"
echo "${LOG_PREFIX} pruning dumps older than ${RETENTION} days"
DELETED=$(find /backups/postgres -name '*.dump' -type f -mtime "+${RETENTION}" -print -delete | wc -l | tr -d ' ')
echo "${LOG_PREFIX} pruned ${DELETED} old dump(s)"

echo "${LOG_PREFIX} OK"
