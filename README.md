# gym_app — альтернатива Zebroid

Веб-сервис для массового постинга на WordPress через XML-RPC. Заменяет
десктопный Zebroid: лёгкий, многопоточный, без виртуальных браузеров,
с прозрачным мониторингом и отказоустойчивостью.

## Документация

Полные планы и архитектурные решения — в [`plans/`](./plans/README.md).
Читать в порядке: `00_overview` → `architecture_decisions` → этапы.

## Быстрый старт (локальная разработка)

Требования:

- Docker Desktop 4.30+ (с выделенными ≥ 6 ГБ RAM и ≥ 4 CPU в Settings → Resources)
- `make` (входит в macOS из коробки)

```bash
# 1. Скопировать .env (один раз)
cp .env.example .env
# Открой .env и поменяй POSTGRES_PASSWORD, JWT_SECRET, SUPER_ADMIN_PASSWORD

# 2. Создать хостовые папки для данных (один раз)
make init-host

# 3. Поднять стек
make up

# 4. Дождаться, пока все сервисы станут healthy (~30 сек)
make ps

# 5. Проверить
curl http://localhost:28080/health
open http://localhost:25173        # UI
open http://localhost:28000        # nginx (UI + API через прокси)
```

## Команды

| Команда                | Что делает                                                       |
|------------------------|------------------------------------------------------------------|
| `make init-host`       | Создаёт `./data/{postgres,redis,minio}` и `./backups/`           |
| `make up`              | Поднимает весь стек в фоне                                       |
| `make down`            | Останавливает стек (данные сохраняются)                          |
| `make logs`            | Тейл логов всех сервисов                                         |
| `make logs-app`        | Только логи backend                                              |
| `make ps`              | Список сервисов и их статус                                      |
| `make shell-app`       | Bash в контейнере backend                                        |
| `make shell-db`        | psql в контейнере Postgres                                       |
| `make migrate`         | Накатить миграции Alembic (появится в этапе 1)                   |
| `make seed`            | Создать super_admin (появится в этапе 1)                         |
| `make test`            | Запустить тесты (появится в этапе 1)                             |
| `make lint`            | Запустить ruff + prettier                                        |
| `make backup-now`      | Прямо сейчас запустить бэкап (вне расписания)                    |
| `make prune`           | Почистить ненужные docker-образы (НЕ трогает данные)             |
| `make reset-db`        | Пересоздать БД (НЕ удаляет `./data/postgres`)                    |

**Сознательно НЕТ:** `make nuke`, `make clean-all`, любой `down -v`.
Хочешь снести данные — `rm -rf ./data ./backups` явной командой
после `make down`. См. [ADR-014](plans/architecture_decisions.md).

## Порты

Все порты на хосте — нестандартные, чтобы не конфликтовать с другими
docker-проектами. Биндятся только на `127.0.0.1`. Меняются через `.env`.

| Сервис     | По умолчанию | Env var              |
|------------|--------------|----------------------|
| Postgres   | 25432        | `DB_PORT_HOST`       |
| PgBouncer  | 26432        | `PGBOUNCER_PORT_HOST`|
| Redis      | 26379        | `REDIS_PORT_HOST`    |
| App API    | 28080        | `APP_PORT_HOST`      |
| UI (Vite)  | 25173        | `UI_PORT_HOST`       |
| Nginx      | 28000        | `NGINX_PORT_HOST`    |

## Структура

```
.
├── plans/                  ← документы по проекту
├── backend/                ← FastAPI app
├── ui/                     ← SvelteKit
├── docker/                 ← Dockerfile-ы и nginx-конфиги
├── deploy/                 ← скрипты бэкапа, maintenance, документы
├── data/                   ← persistent данные (gitignored, см. ADR-014)
├── backups/                ← бэкапы (gitignored)
├── docker-compose.yaml     ← базовый стек
├── docker-compose.dev.yaml ← override для локалки (порты, --reload, mounts)
└── Makefile
```

## Решение проблем

- **Порт занят** (`bind: address already in use`) — поменяй
  `*_PORT_HOST` в `.env`. По умолчанию мы используем нестандартные, но
  если у тебя другой проект на 28080 — поправь.
- **Postgres падает с `permission denied`** на `./data/postgres` —
  запусти `make init-host` (создаст с правильным UID).
- **`make up` зависает на pulling** — медленный интернет, подожди или
  `Ctrl+C` и `make up` снова (продолжится).
- **`make down && make up` потерял данные** — этого НЕ должно быть.
  Если случилось, проверь что в compose volumes указаны через `./data/...`,
  а не как named volumes. См. ADR-014.
- **Поменял `.env`, но контейнер использует старые значения** —
  `docker compose restart` НЕ перечитывает env-переменные. Нужно
  `make down && make up` или `docker compose up -d --force-recreate <service>`.
