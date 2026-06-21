# Восстановление из бэкапа

## Содержимое `./backups/`

```
backups/
├── postgres/
│   ├── 2026-05-15_0330.dump
│   ├── 2026-05-17_0330.dump
│   └── ...                       (retention 7 дней)
├── minio/                        (этап 2)
│   ├── text-items/
│   └── results/
└── log/
    ├── backup.log
    └── prune.log
```

## Postgres

### Полное восстановление

```bash
# 1. Остановить пишущие сервисы (БД оставляем работать)
docker compose stop app

# 2. Дроп + создание чистой БД
docker compose exec db dropdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"

# 3. Накатить дамп. Внутри контейнера backup есть pg_restore — используем его.
docker compose exec backup bash -c '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --host=db \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --no-owner --no-acl \
    --verbose \
    /backups/postgres/2026-05-15_0330.dump
'

# 4. Запустить app обратно
docker compose start app
```

### Восстановление одной таблицы

```bash
# pg_restore поддерживает выборочное восстановление по имени таблицы
docker compose exec backup bash -c '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --host=db \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --table=admin_users \
    --data-only \
    /backups/postgres/2026-05-15_0330.dump
'
```

## MinIO (этап 2)

```bash
docker compose exec backup mc mirror --overwrite \
    /backups/minio/text-items/ minio/text-items
docker compose exec backup mc mirror --overwrite \
    /backups/minio/results/ minio/results
```

## Проверка после восстановления

```bash
# Health backend
curl http://localhost:28080/health

# Логин под super_admin (если данные восстановлены целиком)
curl -X POST http://localhost:28080/admin/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"YOUR_PASSWORD"}'
```

## Ежемесячная проверка восстановления

См. ADR-012: раз в месяц поднимаем отдельный compose-стек на чистой
папке `./restore-test/data/`, накатываем последний дамп, прокликиваем
smoke-сценарий, фиксируем дату в `last_restore_check.md` рядом.

## Off-site (этап 1.5+)

Когда подключим прод-сервер — добавим `OFFSITE_BACKUP_TARGET` в `.env`
и rclone в `run.sh`. Восстановление из off-site — сначала `rclone copy`
вниз в `./backups/`, дальше как выше.
