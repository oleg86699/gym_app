# Backup + maintenance контейнер.
# Внутри: pg_dump, mc (MinIO client), docker-cli, cron.

FROM alpine:3.20

# Postgres 16 client + базовые утилиты + tzdata для cron
RUN apk add --no-cache \
    bash \
    coreutils \
    findutils \
    tzdata \
    curl \
    postgresql16-client \
    docker-cli

# MinIO client (mc)
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
      x86_64)  MC_ARCH=amd64 ;; \
      aarch64) MC_ARCH=arm64 ;; \
      *)       echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://dl.min.io/client/mc/release/linux-${MC_ARCH}/mc" \
      -o /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc

# Скрипты бэкапа и обслуживания
COPY deploy/backup/run.sh /usr/local/bin/run.sh
COPY deploy/maintenance/prune.sh /usr/local/bin/prune.sh
COPY docker/backup-entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/run.sh /usr/local/bin/prune.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
