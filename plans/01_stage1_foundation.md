# Этап 1. Фундамент: docker, git, FastAPI, SvelteKit, RBAC, пользователи

## Цель этапа

Получить на руках работающий каркас, в котором уже:
- Запускается локально через `docker compose up` без ручных шагов.
- Развёртывается на dev-сервер по `git push develop` и на prod-сервер по
  `git push main`.
- Есть админ-панель с собственным дизайном (не повторяющим server_app), где
  можно залогиниться `super_admin`-ом, создать пользователя, группу, роль,
  выдать права, открыть пользователю доступ к конкретным страницам.
- Каждый юзер может зайти в свой профиль и поменять пароль / email.

После этого этапа в коде ещё **нет** ничего про постинг, проекты, тексты,
WordPress. Только инфраструктура и управление доступом. Это намеренно — даёт
проверить деплой и RBAC до того, как мы начнём строить тяжёлую часть.

## Что входит

1. Bootstrap репозитория и инструментов
2. Docker Compose (dev + prod overrides)
3. CI/CD на GitHub Actions
4. FastAPI app skeleton + конфиг + DB + миграции
5. Модели и API RBAC (users, groups, roles, permissions, pages)
6. Seed `super_admin` пользователя из env
7. Auth: login / logout / me / change-password / change-email
8. SvelteKit shell + новый дизайн админки
9. UI-страницы: Login, Dashboard (пустая), Users, Groups, Roles, Pages,
   Profile
10. Page-access middleware на бэке + скрытие в меню на фронте
11. Smoke-e2e тест: логин → создать пользователя → выдать роль → залогиниться
    под новым

## Что НЕ входит (отложено в этап 2+)

- Проекты, прогоны, тексты, админки WordPress
- XML-RPC клиент, постинг, валидатор
- Прокси, нотификации, SSE, дешборды
- Developer API и API keys
- MinIO (понадобится только в этапе 2)

---

## 1. Bootstrap репозитория

### 1.1 Создать `/Volumes/profile/github/gym/app/` и инициализировать

```bash
mkdir -p /Volumes/profile/github/gym/app
cd /Volumes/profile/github/gym/app
git init -b main
```

### 1.2 Базовые файлы в корне

- `.gitignore` (Python + Node + IDE + Docker)
- `.gitattributes` (LF для текста, lockfile линки)
- `.python-version` → `3.12`
- `.nvmrc` → `22`
- `.editorconfig`
- `README.md` — что это, как запустить локально, ссылка на `plans/`
- `LICENSE` (MIT или Proprietary — спросить)
- `.env.example` — все env-переменные с пустыми значениями и комментариями
- `Makefile`:
  - `make init-host` — создать `./data/postgres`, `./data/minio`,
    `./data/redis`, `./backups/` с правильными правами
  - `make up`, `make down` (намеренно **БЕЗ** `-v` флага, см. ADR-014)
  - `make logs`, `make migrate`, `make test`, `make lint`, `make seed`
  - `make backup-now` — ручной запуск бэкапа: `docker compose exec backup
    /usr/local/maintenance/run.sh`
  - `make prune` — ручной docker cleanup (image/builder/container/network
    prune, никогда не volume)
  - `make reset-db` — дроп + создание чистой БД через `dropdb/createdb`,
    БЕЗ удаления `./data/postgres`
  - target-а вроде `make nuke` или `make clean-all`, который удаляет
    `./data/`, **сознательно нет**. Хочешь снести — `rm -rf ./data` руками
- Папка `plans/` — копию текущей или симлинк на `gym/plans/`. Решим:
  переносим этот документ внутрь нового репо.

### 1.3 Pre-commit

`.pre-commit-config.yaml` с:
- `ruff`, `ruff-format` (Python)
- `prettier`, `eslint` (JS/Svelte)
- `detect-secrets`
- `end-of-file-fixer`, `trailing-whitespace`, `check-yaml`,
  `check-merge-conflict`
- `conventional-pre-commit` (commit-msg хук)

### 1.4 Бренчи

- `main` — защищённая, мерж только из `develop`.
- `develop` — защищённая, мерж из feature-веток через PR.
- Branch protection rules выставить руками в GitHub после первого пуша.

---

## 2. Docker Compose

### 2.1 Базовый `deploy/docker-compose.yaml`

Сервисы:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD }
    command: >
      postgres
        -c max_connections=200
        -c shared_buffers=512MB
        -c effective_cache_size=1536MB
        -c work_mem=16MB
        -c maintenance_work_mem=128MB
        -c wal_compression=on
    healthcheck: pg_isready
    volumes:
      - ./data/postgres:/var/lib/postgresql/data   # bind mount, см. ADR-014

  pgbouncer:
    image: edoburu/pgbouncer:latest
    environment:
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      POOL_MODE: transaction
      MAX_CLIENT_CONN: 500
      DEFAULT_POOL_SIZE: 40
    depends_on: { db: { condition: service_healthy } }
    healthcheck: psql -h 127.0.0.1 -p 5432 pgbouncer -U pgbouncer -c "SHOW POOLS"

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    healthcheck: redis-cli ping
    volumes:
      - ./data/redis:/data                          # bind mount, AOF на хосте

  app:
    build: { context: .., dockerfile: docker/app.Dockerfile }
    depends_on: [db (healthy), redis (healthy)]
    env_file: .env
    command: |
      sh -c "alembic upgrade head &&
             uvicorn main:create_app --factory --host 0.0.0.0 --port 8080"
    healthcheck: GET /health

  # TaskIQ воркер (этап 1: только базовые задачи, например password reset email
  # placeholder). Можем не запускать в первом релизе, оставить плейсхолдером.
  taskiq-worker:
    build: { context: .., dockerfile: docker/app.Dockerfile }
    depends_on: [redis (healthy)]
    command: taskiq worker workers.taskiq:broker --workers 2

  ui:
    build:
      context: ../ui
      dockerfile: ../docker/ui.Dockerfile
      target: prod
    # В dev — target: dev с vite, в prod — target: prod со сборкой и static-serv

  nginx:
    image: nginx:1.27-alpine
    depends_on: [app, ui]
    volumes: nginx config (RO)
    healthcheck: GET /health

  backup:
    build: { context: .., dockerfile: docker/backup.Dockerfile }
    depends_on:
      db: { condition: service_healthy }
      # minio: { condition: service_healthy }   # появится в этапе 2
    env_file: .env
    environment:
      TZ: Europe/Kyiv
      BACKUP_CRON: "30 3 */2 * *"               # каждые 48 ч в 03:30
      PRUNE_CRON: "0 4 * * 0"                   # вс 04:00 — docker cleanup
      RETENTION_DAYS: 7
    volumes:
      - ./data/postgres:/var/lib/postgresql/data:ro       # ro — только читать
      # - ./data/minio:/data/minio:ro                     # появится в этапе 2
      - ./backups:/backups                                # rw — сюда пишем
      - /var/run/docker.sock:/var/run/docker.sock         # для prune.sh
      - ./deploy/maintenance:/usr/local/maintenance:ro    # скрипты
    # Внутри контейнера: alpine + postgresql-client + mc + docker-cli + cron;
    # entrypoint запускает crond + тейлит /backups/log
```

**Volumes — никаких named volumes.** Всё через bind mounts в `./data/`
и `./backups/` (см. ADR-014). Папки создаются скриптом
`deploy/init-host.sh` перед первым `make up`.

**Примечание.** В этапе 1 нет MinIO (он приходит в этапе 2), поэтому в
первой версии backup-сервис делает только `pg_dump`. MinIO-mirror
включается в этапе 2 — это +20 строк к существующему скрипту, не отдельная
ветка.

Backup-контейнер одновременно выполняет функцию **maintenance**: cron
`PRUNE_CRON` раз в неделю запускает `deploy/maintenance/prune.sh`
(docker image/builder/container/network prune, никогда не volume —
см. ADR-013). Доступ к docker daemon через mount socket-а.

### 2.1.1 Хостовый `daemon.json` (для серверов)

В README сервера документируется требование к `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

На прототипе локально (macOS Docker Desktop) — настраивается в Preferences →
Docker Engine, тот же JSON.

### 2.2 `deploy/docker-compose.dev.yaml` (override)

- Открывает порты `db`, `redis`, `app`, `ui` на хост для удобства локалки.
- `app` запускается с `--reload`.
- `ui` — vite dev server c HMR, прокси на app.
- Bind-mount `./backend/src:/app/src` и `./ui:/ui` для live edits.

### 2.3 `deploy/docker-compose.deploy.yaml` (override для серверов dev/prod)

- Поднимает образы из GHCR (`image: ghcr.io/...:${VERSION}`) вместо локальной сборки.
- На prod порты только на `127.0.0.1`, наружу торчит только nginx.
- Restart policy `unless-stopped` везде.

### 2.4 Dockerfile-ы

#### `docker/app.Dockerfile`

```
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS base
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/
RUN uv sync --frozen --no-dev
COPY backend/src/ /app/src/
EXPOSE 8080
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
```

#### `docker/ui.Dockerfile` (multi-stage: dev + prod)

```
FROM node:22-alpine AS deps
WORKDIR /app
COPY ui/package.json ui/package-lock.json /app/
RUN npm ci

FROM deps AS dev
COPY ui /app
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM deps AS build
COPY ui /app
RUN npm run build

FROM nginx:1.27-alpine AS prod
COPY --from=build /app/build /usr/share/nginx/html
COPY docker/nginx/ui.conf /etc/nginx/conf.d/default.conf
```

### 2.5 Nginx

`docker/nginx/conf.d/default.conf` (внутренний reverse proxy):
- `/admin/api/*` → `app:8080`
- `/api/v1/*` → `app:8080`
- `/health` → `app:8080`
- `/*` → `ui:80` (SPA fallback `/index.html`)
- SSE: для `/admin/api/*/events` отключить буферизацию.

---

## 3. CI/CD

### 3.1 `.github/workflows/checks.yml`

Срабатывает на PR в `develop` и `main`, на push в эти бренчи, на manual.
Джобы (повторяем server_app):

- **pr-governance** — проверяет conventional commit title, что PR в `main`
  только из `develop`, что PR в `develop` из `feat/`, `fix/`, `chore/`,
  `refactor/`, `docs/`, `test/`, `ci/`, `dependabot/`.
- **backend-quality**:
  - `uv sync --frozen --all-extras --dev`
  - `uv lock --check`
  - `ruff check`, `ruff format --check`
  - `mypy src` (на затронутых файлах через `mypy --files-from`)
  - `pytest -q backend/tests`
- **ui-quality**:
  - `npm ci`
  - `npm run lint`
  - `npm run check` (svelte-check)
  - `npm run test:unit`
  - `npm run build` (проверка сборки)
- **e2e-smoke**:
  - Поднимает compose в CI runner
  - Запускает Playwright: открыть `/login`, войти `admin`, увидеть Dashboard

### 3.2 `.github/workflows/deploy-dev.yml`

Срабатывает на push в `develop` или manual.

Стадии:
1. **prepare** — вычисляет semver от последнего тега (логика из server_app).
2. **build-and-push** — собирает 2 образа (`app`, `ui`), пушит в GHCR с
   тегами `{version}` и `dev-latest`.
3. **deploy-dev**:
   - SSH на dev-сервер по ключу из `secrets.DEV_SSH_KEY`.
   - Доставляет `deploy/docker-compose.yaml`, `deploy/docker-compose.deploy.yaml`.
   - `docker compose pull && docker compose up -d`.
   - Health-check POST к `/health`, в случае фейла — откат на предыдущий
     `version` (тег `dev-prev`).

### 3.3 `.github/workflows/deploy-prod.yml`

Срабатывает на push в `main` или manual.
Аналогично deploy-dev, но:
- Использует `secrets.PROD_SSH_KEY`, `vars.PROD_DEPLOY_ROOT`.
- Semver без `-dev.{run}` суффикса.
- Перед deploy создаёт GitHub Release с changelog (`git-cliff` или вручную).
- Возможность задать второй сервер через workflow matrix (на старте matrix
  состоит из одного сервера — закладываем расширяемость).

### 3.4 Secrets и переменные

В GitHub репо настроить:
- **Secrets** (environment `development` и `production`):
  - `*_SSH_KEY`, `*_SSH_HOST`, `*_SSH_USER`
  - `APP_ENV_CONTENT` (полный `.env` для контейнеров)
- **Variables**:
  - `DEV_DEPLOY_ROOT`, `PROD_DEPLOY_ROOT`, `APP_NAME`

### 3.5 Скрипт preflight-connections

`deploy/scripts/preflight-connections.sh` — проверяет, что все нужные secrets
заданы и SSH доступен; выполняется первым шагом в deploy workflow.

---

## 4. FastAPI app skeleton

### 4.1 Зависимости (`backend/pyproject.toml`)

Минимум для этапа 1:
```
fastapi, uvicorn[standard], pydantic, pydantic-settings, email-validator,
sqlalchemy[asyncio], asyncpg, alembic, passlib[bcrypt], pyjwt, python-multipart,
httpx, taskiq, taskiq-redis, redis, prometheus-fastapi-instrumentator
```

Dev:
```
pytest, pytest-asyncio, httpx (для test client), testcontainers[postgres,redis],
ruff, black, mypy, types-passlib
```

### 4.2 `backend/src/main.py`

```python
from fastapi import FastAPI
from core.config import settings
from api.admin.routes import api_router as admin_router
from api.public.routes import api_router as public_router
from api.middleware.errors import register_exception_handlers
from api.middleware.cors import setup_cors

def create_app() -> FastAPI:
    app = FastAPI(title="Postzebra API", version=settings.VERSION)
    setup_cors(app)
    register_exception_handlers(app)
    app.include_router(public_router)            # /health, /admin/api/auth/login
    app.include_router(admin_router, prefix="/admin/api")
    return app
```

### 4.3 `core/config.py`

`pydantic-settings`, читает `.env`. Все секреты/URL:
```
DATABASE_URL, REDIS_URL, JWT_SECRET, JWT_ALG, JWT_TTL_HOURS,
SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD, SUPER_ADMIN_EMAIL,
ALLOWED_ORIGINS, ENVIRONMENT (dev|prod), LOG_LEVEL
```

### 4.4 `core/db.py`

Async engine + sessionmaker (`async_sessionmaker`). DI зависимости:

```python
async def get_db_write() -> AsyncIterator[AsyncSession]:
    async with write_session_factory() as session:
        yield session

async def get_db_read() -> AsyncIterator[AsyncSession]:
    async with read_session_factory() as session:
        yield session
```

На этапе 1 обе фабрики смотрят на один и тот же `DATABASE_URL` (через
PgBouncer). Разделение API контракта закладывается сразу — в будущем
`DATABASE_READ_URL` сможет указывать на read replica без изменения
сервисного кода (см. ADR-011, категория Б).

В сервисах используется `get_db_read` по умолчанию; `get_db_write`
запрашивается явно в мутирующих операциях.

### 4.4.1 `core/logging.py`

`structlog` с JSON-renderer-ом, `trace_id` middleware:

```python
@app.middleware("http")
async def trace_id_mw(request, call_next):
    trace_id = request.headers.get("X-Trace-Id", uuid4().hex)
    structlog.contextvars.bind_contextvars(trace_id=trace_id, path=request.url.path)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response
```

`trace_id` пробрасывается в Celery/TaskIQ через `before_publish` / `before_task`
signal-ы. В логах всегда виден сквозной id запроса от HTTP до воркера.

### 4.4.2 Базовый mixin для моделей

`infrastructure/db/base.py`:

```python
class TimestampedMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)

class SoftDeletableMixin(TimestampedMixin):
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
```

Все боевые модели (`AdminUser`, `Project`, `PostingRun`, ...) наследуют
один из этих миксинов. Все default-ные сервис-запросы фильтруют по
`deleted_at IS NULL` (см. ADR-003 связанную конвенцию).

### 4.4.3 Конвенция пагинации и стриминга

`api/common/pagination.py` — типизированный helper для cursor-based
пагинации:

```python
class Cursor(BaseModel):
    after_id: int | None
    limit: int = 50  # max 200

class PaginatedResponse(GenericModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
```

Все list-эндпоинты используют этот контракт. `next_cursor` — opaque base64
строка с `(after_id, optional_after_created_at)`.

Большие выгрузки (CSV прогона, бэкап) возвращают `StreamingResponse` с
асинхронным курсором по БД, не материализуя в память. Helper
`api/common/streaming.py`.

### 4.5 `core/security.py`

- `hash_password(plain) -> str` (bcrypt)
- `verify_password(plain, hashed) -> bool`
- `create_access_token(payload, ttl) -> str` (PyJWT)
- `decode_access_token(token) -> dict | None`

### 4.6 Alembic

`backend/src/migrations/`:
- `env.py` — берёт `DATABASE_URL` из `core.config`, импортирует `Base.metadata`.
- `alembic.ini`.
- Первая миграция — пустая (нужна для baseline-а), вторая — все таблицы RBAC.

---

## 5. RBAC модели

См. `architecture_decisions.md` (ADR-005) для общей схемы. Таблицы:

```
admin_users
  id, username, email, hashed_password, is_active, full_name,
  group_id (FK admin_groups, nullable),
  created_at, last_login_at

admin_groups
  id, name (uq), description, is_active, created_at

admin_roles
  id, name (uq), description, is_active, is_system (bool — для защиты super_admin)

admin_permissions
  id, code (uq, e.g. 'users.create'), resource, action, description

admin_pages
  id, path (uq, e.g. '/users'), name, description, is_active

-- pivot
user_roles (admin_user_id, role_id) PK both
role_permissions (role_id, permission_id) PK both
role_pages (role_id, page_id) PK both
user_pages (admin_user_id, page_id) PK both
```

Уникальные индексы и FK с `ON DELETE CASCADE` где уместно.

### Seed-данные (alembic data migration)

- Создать роль `super_admin` (`is_system=true`).
- Создать роль `group_admin` (`is_system=true`).
- Создать роль `manager` (`is_system=true`, дефолт для новых юзеров).
- Создать страницы из known-set: `/dashboard`, `/users`, `/groups`, `/roles`,
  `/pages`, `/profile`. Привязать все к `super_admin`. Привязать `/dashboard`
  и `/profile` ко всем ролям.
- Базовый набор permissions: `users.view`, `users.create`, `users.edit`,
  `users.delete`, `groups.*`, `roles.*`, `pages.*`. Привязать все к
  `super_admin`. `users.view` (только в своей группе) — к `group_admin`.

### Seed super_admin пользователя

Отдельный idempotent скрипт `backend/src/scripts/seed_super_admin.py`,
запускается в Docker entrypoint:
```python
# если по env SUPER_ADMIN_USERNAME нет юзера — создать с паролем
# из SUPER_ADMIN_PASSWORD и ролью super_admin
```

---

## 6. Бэкенд API этапа 1

Все эндпоинты под `/admin/api/`.

### 6.1 Auth (`/auth`)

- `POST /login` — body `{username, password}`, ставит cookie + возвращает
  `{access_token, user}`. Записывает `last_login_at`.
- `POST /logout` — очищает cookie.
- `GET /me` — возвращает текущего юзера + его роли + permissions + доступные
  страницы.
- `PATCH /me/password` — `{current_password, new_password}`. Проверяет
  текущий, хеширует новый.
- `PATCH /me/email` — `{email, current_password}`. С проверкой пароля.

### 6.2 Users (`/users`)

Доступ: `super_admin` — все юзеры; `group_admin` — юзеры своей группы.
- `GET /users` — список с пагинацией, фильтр по group_id.
- `POST /users` — создать (username, email, password, group_id, role_ids).
- `GET /users/{id}` — детали.
- `PATCH /users/{id}` — обновить (full_name, email, is_active, group_id,
  role_ids). Пароль отдельным эндпоинтом ниже.
- `DELETE /users/{id}` — soft delete (is_active=false) или hard delete
  для super_admin. Защита: нельзя удалить себя; нельзя удалить последнего
  super_admin.
- `POST /users/{id}/reset-password` — устанавливает новый пароль
  (super_admin / group_admin).

### 6.3 Groups (`/groups`)

`super_admin` only.
- `GET /groups`, `POST /groups`, `PATCH /groups/{id}`, `DELETE /groups/{id}`.

### 6.4 Roles (`/roles`)

`super_admin` only. Системные роли (`is_system=true`) — нельзя удалить;
у `super_admin` нельзя менять permissions/pages.
- `GET /roles`, `POST /roles`, `PATCH /roles/{id}` (включает обновление
  `permission_ids` и `page_ids`), `DELETE /roles/{id}`.

### 6.5 Permissions (`/permissions`)

`super_admin` only.
- `GET /permissions` — список всех (только чтение; permissions создаются
  миграциями кода, не пользователями).

### 6.6 Pages (`/pages`)

`super_admin` only.
- `GET /pages` — список всех страниц + кому открыты.
- `PATCH /pages/{id}` — обновить `is_active`, `role_ids`, `user_ids`.
- Endpoint `GET /pages/me` — какие страницы доступны мне сейчас (для меню UI).

### 6.7 Middleware

- `get_current_user` (auth dep)
- `require_role("super_admin")` (Depends factory)
- `require_permission("users.create")` (Depends factory)
- `require_page_access(request)` — берёт `request.url.path`, проверяет, что
  он есть в `me.accessible_pages`. Используется на каждом защищённом
  роутере как router-level dependency.

### 6.8 Error handling

Единый JSON-ответ ошибки:
```json
{ "error": { "code": "permission_denied", "message": "..." } }
```

---

## 7. SvelteKit UI

### 7.1 Bootstrap

```bash
cd /Volumes/profile/github/gym/app/ui
npm create svelte@latest .
# Skeleton, TypeScript, ESLint, Prettier, Vitest, Playwright
npm i -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
```

### 7.2 Структура

```
ui/src/
├── app.html, app.css (tailwind directives), app.d.ts
├── hooks.server.ts (read cookie → load user → locals.user)
├── hooks.client.ts (global error handler)
├── lib/
│   ├── api/
│   │   ├── client.ts        # fetch-обёртка с авторизацией и обработкой ошибок
│   │   ├── auth.ts          # login, logout, me, changePassword, changeEmail
│   │   ├── users.ts
│   │   ├── groups.ts
│   │   ├── roles.ts
│   │   ├── pages.ts
│   │   └── types.ts         # сгенерированные типы (или вручную)
│   ├── components/
│   │   ├── layout/
│   │   │   ├── AdminShell.svelte
│   │   │   ├── Sidebar.svelte
│   │   │   ├── Topbar.svelte
│   │   │   └── UserMenu.svelte
│   │   ├── ui/              # кнопки, формы, тосты (TailwindCSS-based)
│   │   └── data-table/      # переиспользуемая таблица с пагинацией
│   ├── stores/
│   │   ├── user.ts          # writable<User | null>
│   │   ├── permissions.ts
│   │   └── toast.ts
│   └── utils/
├── params/
└── routes/
    ├── +layout.svelte                  # глобальный layout (toast, favicon)
    ├── login/+page.svelte               # форма логина
    ├── (app)/                          # требует авторизации (load guard)
    │   ├── +layout.ts                  # load: getMe(), редирект на /login если 401
    │   ├── +layout.svelte              # AdminShell
    │   ├── dashboard/+page.svelte      # заглушка
    │   ├── users/
    │   │   ├── +page.ts                # load: api.users.list()
    │   │   ├── +page.svelte            # таблица
    │   │   └── [id]/
    │   │       ├── +page.ts
    │   │       └── +page.svelte        # форма редактирования
    │   ├── groups/...
    │   ├── roles/...
    │   ├── pages/...                   # матрица доступа
    │   └── profile/+page.svelte        # смена пароля и email
    └── +error.svelte                   # 401/403/404/500
```

### 7.3 Дизайн (новый, не из server_app)

Cтиль: **минималистичный**, светлая и тёмная темы. Палитра нейтральная
(slate + один акцентный — индиго/изумруд/янтарный — выбрать). Шрифт: Inter.

- Sidebar слева, шириной ~240px, сворачивается до 64px.
- Topbar с хлебными крошками + быстрым поиском + user-menu (профиль/logout).
- Таблицы — собственный лёгкий компонент с пагинацией и поиском. Не
  тащим DataTables/jQuery.
- Формы — кастомные input/select на Tailwind. Опционально подключаем
  `bits-ui` или `melt-ui` для модалок/dropdown/dialog.
- Тосты — `svelte-french-toast` или собственный простой стор.

В отдельном файле `plans/01_stage1_ui_design.md` (создаётся в начале
работы над UI этапа 1) — детальные мокапы каждой страницы.

### 7.4 Авторизация и страницы

`hooks.server.ts`:
- Читает cookie `admin_token`, делает запрос на `/admin/api/auth/me`,
  кладёт юзера и его pages/permissions в `event.locals.user`.
- Если cookie невалидный — `event.locals.user = null`.

`routes/(app)/+layout.ts`:
- Если `locals.user === null` → `throw redirect(307, '/login')`.

Каждая страница в `routes/(app)/`:
- Имеет `+layout.ts` или `+page.ts`, который дополнительно проверяет, есть
  ли путь в `user.accessible_pages`. Если нет — `throw error(403)`.

Sidebar показывает только пункты меню, которые есть в `user.accessible_pages`.

### 7.5 Страница Pages (матрица доступа)

`/pages` — матрица:
- Строки: страницы (`/dashboard`, `/users`, ...).
- Колонки: роли + индивидуальные юзеры (вкладки).
- Ячейки: чекбоксы (есть доступ / нет).
- Сохранение — `PATCH /admin/api/pages/{id}` с массивом role_ids/user_ids.

### 7.6 Профиль

`/profile`:
- Ник + дата создания (read-only).
- Email — редактируется (с подтверждением пароля).
- Смена пароля — форма (current, new, confirm).

---

## 8. Тестирование этапа 1

### 8.1 Бэкенд unit/integration

- Тесты сервисов: `auth_service.authenticate`, `user_service.create_user`,
  `permission_service.check`.
- Тесты эндпоинтов через `httpx.AsyncClient + ASGITransport`.
- DB — `testcontainers.postgres`, мигрируется per-session, чистится
  truncate-ом между тестами.

### 8.2 UI

- Vitest на компоненты (форма логина — валидация email, disabled при
  пустом пароле).
- Playwright e2e:
  - Логин под `admin` → видим Dashboard.
  - Создать пользователя `user1` с ролью `manager` → видим в таблице.
  - Выдать ему доступ к странице `/users` → залогиниться под ним → ссылка
    видна и страница открывается.
  - Сменить пароль из профиля → разлогиниться → войти с новым.

### 8.3 Smoke на CI

В `checks.yml` e2e-smoke поднимает compose и гоняет ключевые сценарии.

---

## 9. Документация

В `app/README.md`:
- Зависимости (docker, make, mkcert если HTTPS локально).
- Команды: `make up`, `make migrate`, `make seed`, `make test`.
- Дефолтный логин (из `.env.example`).
- Как добавить новый бэкенд-эндпоинт.
- Как добавить новую страницу в UI и зарегистрировать её в pages-таблице.

В `app/CONTRIBUTING.md`:
- Бренчевание, conventional commits, как открывать PR.

---

## 10. Критерии приёмки этапа

Этап считается завершённым, когда:

1. `git clone` + `cp .env.example .env` + `make up` → стек поднимается без
   ручных шагов. Health-check всех контейнеров зелёный.
2. По адресу `http://localhost:5173` открывается логин. Логин `admin` /
   пароль из `.env` пускает на дашборд.
3. Все CRUD страницы (Users, Groups, Roles, Pages) работают через UI.
4. Включена и проверена иерархия доступа:
   - `super_admin` видит всех юзеров и все группы.
   - `group_admin` (созданный в UI и назначенный группе) видит только
     юзеров своей группы.
   - `manager` (созданный в UI) видит только страницы `Dashboard` и `Profile`,
     остальное скрыто из меню и закрыто на бэке.
5. Профиль (`/profile`) позволяет сменить пароль и email; смена пароля
   валидирует текущий.
6. Push в `develop` → деплой на dev-сервер; push в `main` (после мерджа PR
   из `develop`) → деплой на prod-сервер. Оба сервера отвечают по своим
   URL, логин работает.
7. CI checks зелёные на PR.
8. e2e Playwright smoke зелёный на CI.
9. **Бэкап работает**: после первой ночи (или ручного запуска
   `make backup-now`) в `./backups/postgres/` лежит свежий `.dump`.
   Восстановление по `RESTORE.md` поднимает рабочую копию приложения
   с теми же данными.
10. **Персистентность доказана**: `make down && make up` после нескольких
    раз НЕ теряет ни юзеров, ни их пароли, ни роли. После `docker system
    prune -a -f` (без `--volumes`) на хосте — то же самое, всё на месте.
11. **Дисковая гигиена работает**: `make prune` чистит dangling images
    и старый build cache. В `./data/` после `make prune` ничего не
    изменилось. `make` без таргета, удаляющего `./data/`, не существует.

---

## 11. Декомпозиция на feat-ветки

Каждый bullet — один PR в `develop`. Заводим в порядке зависимости.

1. `chore/bootstrap-repo` — gitignore, editorconfig, pre-commit, README,
   Makefile, .env.example, плейсхолдер `backend/` и `ui/`.
2. `chore/backend-skeleton` — pyproject, uv.lock, FastAPI app factory,
   /health, Dockerfile.
3. `chore/ui-skeleton` — SvelteKit init, Tailwind, базовый layout, /login
   stub, Dockerfile.
4. `chore/docker-compose-dev` — compose базовый + dev override, healthchecks
   на всех сервисах, depends_on `service_healthy`.
5. `chore/pgbouncer-and-pg-tuning` — PgBouncer контейнер, конфиг
   max_connections/shared_buffers/work_mem для Postgres.
6. `chore/ci-checks` — workflow `checks.yml`, лайтовые джобы.
7. `chore/structured-logging-and-trace-id` — structlog JSON, trace_id
   middleware, прокидывание trace_id в TaskIQ контекст.
8. `feat/db-and-migrations` — async engine с двумя session-фабриками
   (write/read), Alembic baseline, базовые mixin-ы Timestamped/SoftDeletable.
9. `chore/streaming-and-pagination-conventions` — cursor-based пагинация,
   StreamingResponse helper, типизированные ответы.
10. `feat/rbac-models-and-seed` — модели, миграция со всеми таблицами,
    seed super_admin + системных ролей и страниц.
11. `feat/auth-endpoints` — login/logout/me/change-password/change-email.
12. `feat/auth-middleware` — JWT, get_current_user, require_role,
    require_permission, require_page_access.
13. `feat/users-crud-api` — endpoints /users.
14. `feat/groups-crud-api` — endpoints /groups.
15. `feat/roles-crud-api` — endpoints /roles + /permissions.
16. `feat/pages-api` — endpoints /pages и /pages/me.
17. `feat/ui-login-and-shell` — страница логина, AdminShell, sidebar с
    меню по `pages/me`, profile-menu.
18. `feat/ui-users-page` — таблица + форма создания/редактирования.
19. `feat/ui-groups-page`.
20. `feat/ui-roles-page` — формы выдачи permissions и pages для роли.
21. `feat/ui-pages-matrix` — матрица page × role/user.
22. `feat/ui-profile-page` — смена email и пароля.
23. `feat/backup-service` — контейнер `backup`, скрипт `run.sh`,
    `RESTORE.md`, ротация 7 дней, cron каждые 48 ч. На этапе 1 только
    Postgres; MinIO-mirror добавляется в этапе 2.
24. `chore/persistent-paths-and-disk-hygiene` — `deploy/PERSISTENT_PATHS.md`,
    `deploy/init-host.sh`, `deploy/maintenance/prune.sh`,
    `deploy/maintenance/compose-lint.sh`, target-ы в Makefile
    (`init-host`, `prune`, `reset-db`), документация по `daemon.json`
    в README сервера. Backup-контейнер расширяется PRUNE_CRON.
25. `feat/deploy-dev-workflow` — `.github/workflows/deploy-dev.yml`,
    подключение к dev-серверу.
26. `feat/deploy-prod-workflow` — то же для prod.
27. `test/e2e-smoke` — Playwright happy-path.

После каждого PR в `develop` — автодеплой на dev-сервер. После каждого
PR из `develop` в `main` — на prod.

---

## 12. Открытые вопросы перед стартом

Зафиксированные ответы:

1. **Где работаем**: на этапе прототипа — **только локально**. Git, GHCR
   и серверы (dev/prod) подключаются позже, отдельной волной. Сейчас
   `feat/deploy-*` и push-в-git задачи **откладываются**.
2. **Лицензия**: проект **приватный**. `LICENSE` файл пока не создаём.
3. **Имя репо в GitHub**: уточняется при подключении к git.
4. **GitHub-аккаунт**: обычный личный аккаунт пользователя.
5. **TLS**: пока не нужен (нет сервера).
6. **`.env`**: создаём `.env.example` с плейсхолдерами и пустой
   `.env` (в .gitignore). Пользователь сам заполнит реальные значения:
   - `SUPER_ADMIN_USERNAME`, `SUPER_ADMIN_PASSWORD` (опц. `SUPER_ADMIN_EMAIL`)
   - `POSTGRES_*`
   - `JWT_SECRET`
   - `REDIS_URL`, `DATABASE_URL`
   - SMTP — пока плейсхолдеры
7. **Локализация**: только **EN** в UI. i18n-обвязку не подключаем,
   текст пишем напрямую. Если позже понадобится RU — обернём в
   `svelte-i18n` отдельной веткой.
8. **Часовой пояс**: **Europe/Kyiv** в контейнерах (`TZ` env), в БД
   все timestamps хранятся как `TIMESTAMPTZ` (UTC по умолчанию у Postgres),
   UI рендерит в браузерной таймзоне.
9. **Бэкапы**: каждые 48 часов в локальную папку `./backups/`, retention 7
   дней. Сервис в compose. См. ADR-012.
10. **`SUPER_ADMIN_EMAIL`** — опционален. Юзер заполняет в профиле.
11. **iOS Safari** и прочие браузеры: вопрос откладывается до этапа 3
    (SSE). На этапе 1 поллинга нет, тестируем в Chrome/Firefox.

### Изменения в декомпозиции из-за «работаем локально»

Пункты 24 (`feat/deploy-dev-workflow`) и 25 (`feat/deploy-prod-workflow`)
**временно вычёркиваются** из этапа 1. Возвращаются как отдельная мини-волна
«stage 1.5: подключение git и деплоя» после того, как этап 1 локально
работает и пройден приёмкой.

Пункт 6 (`chore/ci-checks`) тоже откладывается — без git нет PR, в которых
прогонять checks. Локально pre-commit покрывает базу.
