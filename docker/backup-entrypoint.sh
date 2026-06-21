#!/bin/sh
# Backup-контейнер: формирует crontab из env-переменных и запускает crond.

set -e

mkdir -p /backups/postgres /backups/minio /backups/log

# Дефолты на случай если env не передали
BACKUP_CRON="${BACKUP_CRON:-30 3 */2 * *}"
PRUNE_CRON="${PRUNE_CRON:-0 4 * * 0}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

# Прокидываем env в окружение cron-задач (crond их не видит из родителя)
env | grep -E '^(POSTGRES_|MINIO_|RETENTION_DAYS|TZ)=' > /etc/environment

# Crontab. Каждая задача читает /etc/environment.
cat > /etc/crontabs/root <<EOF
# m h dom mon dow  command
${BACKUP_CRON} . /etc/environment && /usr/local/bin/run.sh >> /backups/log/backup.log 2>&1
${PRUNE_CRON} . /etc/environment && /usr/local/bin/prune.sh >> /backups/log/prune.log 2>&1
EOF

echo "[backup-entrypoint] TZ:               ${TZ:-UTC}"
echo "[backup-entrypoint] Backup schedule:  ${BACKUP_CRON}"
echo "[backup-entrypoint] Prune schedule:   ${PRUNE_CRON}"
echo "[backup-entrypoint] Retention:        ${RETENTION_DAYS} days"
echo "[backup-entrypoint] Starting crond in foreground"

# crond -f держит контейнер живым; -L 8 пишет логи в stderr
exec crond -f -L 8
