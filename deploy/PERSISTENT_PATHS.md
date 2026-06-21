# Persistent paths registry

Единственный источник правды о том, что не должно потеряться. См. ADR-014.

## Хост → Контейнер

| Хостовый путь        | Контейнер                              | Что лежит                                                     | При потере                                              | Бэкап                          |
|----------------------|-----------------------------------------|---------------------------------------------------------------|---------------------------------------------------------|--------------------------------|
| `./data/postgres/`   | `db:/var/lib/postgresql/data`           | Вся БД: users, RBAC, projects, runs, text-items метаданные    | Полная потеря состояния приложения                      | `pg_dump` → `./backups/postgres/` |
| `./data/redis/`      | `redis:/data`                           | Redis AOF: очереди задач, кэш                                 | Потеря только in-flight задач. Состояние из Postgres подберётся (ADR-001) | Не бэкапим                     |
| `./data/minio/`      | `minio:/data` *(этап 2)*                | Контент .txt прогонов, CSV-результаты, загруженные архивы     | Потеря контента; метаданные останутся в Postgres        | `mc mirror` → `./backups/minio/` |
| `./backups/`         | `backup:/backups`                       | Бэкапы Postgres + MinIO mirror                                | Потеря резерва — критично                               | (этап 1.5+) off-site rclone     |
| `./logs/`            | `app:/var/log/app` *(если включим)*     | App JSON logs                                                  | Не критично — то же в docker daemon                     | Не бэкапим                     |

## Правила

1. **Все эти папки в `.gitignore`.** В git попадают только пути в `deploy/init-host.sh`.
2. **Создаются один раз** скриптом `make init-host` перед первым `make up`.
3. **Backup-контейнер монтирует data-папки read-only**, чтобы по ошибке ничего не записать в Postgres data dir.
4. **`docker compose down -v` ЗАПРЕЩЁН.** В Makefile нет такого target-а. `down -v` сносит named volumes, но bind mounts защищены — однако привычку лучше выработать.
5. **`docker volume prune` НИКОГДА не в автоматических скриптах.** `prune.sh` чистит только images / builder cache / containers / networks.

## Если действительно надо снести данные

```bash
make down
rm -rf ./data ./backups       # ритуально, осознанно, руками
make init-host
make up
make migrate
make seed
```

Это **не** автоматизируется ни в одном target-е Makefile.

## Восстановление из бэкапа

См. `deploy/backup/RESTORE.md`.
