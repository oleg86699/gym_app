# Обзор проекта: альтернатива Zebroid

## Назначение

Веб-сервис для автоматизации массового постинга контента на сайты под управлением
WordPress через XML-RPC. Заменяет десктопный софт Zebroid, у которого критические
проблемы с производительностью, утечками памяти и потерей прогресса при крашах.

Полный референс по требованиям бизнеса лежит в `Task Zebroid alternative.txt` —
два блока: первая версия ТЗ от пользователей и обновлённая 2026 года.

## Корень проекта

`/Volumes/profile/github/gym/app/`

Старый Flask-проект (`/Volumes/profile/github/gym/flask/`) остаётся как архив, в
новом коде на него не опираемся, но логика бизнес-процессов и схема MySQL служат
референсом.

## Технологический стек

### Бэкенд
- **Python 3.12+**, менеджер зависимостей **uv**
- **FastAPI** (factory pattern, ASGI)
- **SQLAlchemy 2.0 async** + **asyncpg** драйвер
- **Alembic** для миграций
- **PostgreSQL 16**
- **Redis 7** — кэш + брокер очередей
- **Celery** — тяжёлые длинные задачи постинга (1000+ текстов на 1000+ сайтов)
- **TaskIQ** — лёгкие async-задачи под UI (валидация одной админки, enqueue
  прогона, callback'и, scheduled jobs)
- **httpx** (async) — HTTP-клиент для XML-RPC запросов
- **Pydantic v2** + **pydantic-settings** для конфига и схем
- **PyJWT** + **passlib[bcrypt]** для auth
- **MinIO** (self-hosted S3) — хранилище .txt файлов и результатов

### Фронтенд
- **SvelteKit** (Svelte 5 runes)
- **Vite** dev-сервер
- **TypeScript**
- **TailwindCSS** для стилей (новый дизайн, не похожий на server_app)
- **shadcn-svelte** или **bits-ui** как библиотека компонентов

### Инфраструктура
- **Docker** + **Docker Compose**
- **Nginx** (reverse proxy, TLS termination в продакшене)
- **GitHub Actions** для CI/CD
- **GitHub Container Registry (GHCR)** для образов
- SSH + `docker compose pull` для деплоя на сервера

### Тулинг разработки
- **ruff** + **black** для Python
- **prettier** + **eslint** для JS/Svelte
- **pre-commit** хуки
- **detect-secrets**
- **pytest** + **pytest-asyncio** для бэкенда
- **vitest** + **@playwright/test** для фронтенда
- **mypy** в `strict-on-touched` режиме

## Архитектура (высокий уровень)

```
┌────────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Browser (SK)  │ ──> │    nginx     │ ──> │  FastAPI (app)  │
└────────────────┘     └──────────────┘     └────────┬────────┘
                                                      │
                  ┌───────────────────────────────────┤
                  │                                   │
            ┌─────▼─────┐    ┌──────────────┐   ┌─────▼─────┐
            │ Postgres  │    │    Redis     │   │   MinIO   │
            └───────────┘    └──┬───────┬───┘   └───────────┘
                                │       │
                       ┌────────▼──┐  ┌─▼─────────────┐
                       │  TaskIQ   │  │ Celery worker │
                       │  worker   │  │ (heavy posts) │
                       └───────────┘  └───────────────┘
```

## Структура репозитория

```
gym/app/
├── .github/
│   └── workflows/
│       ├── checks.yml          # lint, typecheck, tests на PR
│       ├── deploy-dev.yml      # push develop -> dev сервер
│       └── deploy-prod.yml     # push main -> prod сервер
├── docker/
│   ├── app.Dockerfile          # бэкенд + celery + taskiq
│   ├── ui.Dockerfile           # SvelteKit (dev и prod targets)
│   └── nginx/
│       ├── nginx.conf
│       └── conf.d/
├── deploy/
│   ├── docker-compose.yaml         # базовый (общие сервисы)
│   ├── docker-compose.dev.yaml     # override для локальной разработки
│   ├── docker-compose.deploy.yaml  # override для серверов (dev/prod)
│   └── scripts/                    # preflight, миграции, бэкапы
├── backend/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── src/
│   │   ├── main.py             # create_app factory
│   │   ├── core/
│   │   │   ├── config.py       # pydantic-settings
│   │   │   ├── security.py     # JWT, хеши
│   │   │   ├── celery_app.py
│   │   │   ├── taskiq_app.py
│   │   │   ├── db.py           # async engine, session
│   │   │   └── storage.py      # MinIO client wrapper
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   ├── admin/          # админка (UI бэк): /admin/api/*
│   │   │   │   ├── routes/
│   │   │   │   ├── middleware/
│   │   │   │   └── schemas.py
│   │   │   ├── v1/             # публичное API для разработчиков
│   │   │   │   ├── routes/
│   │   │   │   └── auth.py     # API keys
│   │   │   └── public/         # эндпоинты без авторизации (health, login)
│   │   ├── domain/             # бизнес-логика (use cases)
│   │   │   ├── users/
│   │   │   ├── projects/
│   │   │   ├── postings/       # прогоны
│   │   │   ├── texts/
│   │   │   ├── wp_accesses/    # пул админок WordPress
│   │   │   └── notifications/
│   │   ├── infrastructure/
│   │   │   ├── db/
│   │   │   │   ├── base.py
│   │   │   │   └── models/     # SQLAlchemy ORM модели
│   │   │   ├── storage/        # MinIO
│   │   │   └── wp_client/      # XML-RPC клиент (httpx-based)
│   │   ├── workers/
│   │   │   ├── celery/         # тяжёлые задачи постинга
│   │   │   └── taskiq/         # лёгкие задачи
│   │   └── migrations/         # Alembic
│   └── tests/
└── ui/
    ├── package.json
    ├── svelte.config.js
    ├── vite.config.ts
    ├── tailwind.config.ts
    ├── src/
    │   ├── app.html
    │   ├── hooks.client.ts
    │   ├── hooks.server.ts
    │   ├── lib/
    │   │   ├── api/            # типизированный клиент к /admin/api/*
    │   │   ├── components/
    │   │   ├── stores/         # auth, user, perms
    │   │   └── utils/
    │   └── routes/
    │       ├── +layout.svelte  # admin shell (sidebar + topbar)
    │       ├── login/
    │       ├── (app)/
    │       │   ├── dashboard/
    │       │   ├── users/
    │       │   ├── groups/
    │       │   ├── roles/
    │       │   ├── pages/      # матрица прав на страницы
    │       │   ├── profile/
    │       │   ├── projects/
    │       │   ├── wp-accesses/
    │       │   └── ...
    │       └── ...
    └── tests/
```

## Этапы разработки

Этапы изолированы — каждый завершается работающим, протестированным куском
функционала, после которого можно сделать паузу и обсудить дальше.

| № | Этап                                     | Файл плана                              |
|---|------------------------------------------|-----------------------------------------|
| 1 | Фундамент: docker, git, FastAPI, RBAC,   | `01_stage1_foundation.md`               |
|   | админ-панель UI, пользователи            |                                         |
| 2 | Ядро постинга: проекты, прогоны,         | `02_stage2_posting_core.md`             |
|   | загрузка текстов и админок, XML-RPC      |                                         |
| 3 | Дешборды, real-time, валидатор админок,  | `03_stage3_dashboards_monitoring.md`    |
|   | прокси, нотификации                      |                                         |
| 4 | Developer API + документация             | `04_stage4_developer_api.md`            |
| 5 | Roadmap: Indexing, Spintax, AI,          | `05_stage5_roadmap_extras.md`           |
|   | главные страницы WP                      |                                         |

Архитектурные решения (хранение файлов, денормализованные счётчики, RBAC модель)
вынесены в отдельный документ: `architecture_decisions.md`.

## Бренчевание и релизы (повтор схемы server_app)

- `main` — продакшен. Только из `develop` через PR.
- `develop` — dev. Из feature-веток через PR.
- Feature-ветки: `feat/...`, `fix/...`, `chore/...`, `refactor/...`, `docs/...`,
  `test/...`, `ci/...`.
- **Conventional Commits** обязательны для title PR; CI проверяет.
- Semver: автоматически инкрементируется по commit-сообщениям в workflow.
  - `BREAKING CHANGE` или `feat!:` → major
  - `feat:` → minor
  - всё остальное → patch
- Образы публикуются в GHCR с тегами `{version}`, `dev-latest`, `latest`.

## Серверы

- **dev**: 1 сервер, выкатывается из `develop`. Доступ по basic IP/порт.
- **prod**: 1 сервер, выкатывается из `main`. За nginx + TLS.
- Расширение на второй prod-сервер заложено в workflow как параметр (matrix), но
  на старте не активируется.

## Глоссарий

- **Прогон / posting run** — один акт массового постинга в рамках одного проекта
  (пачка .txt → пачка успешных публикаций).
- **Админка / WP access** — реквизиты доступа к одному WordPress-сайту
  (`domain;login;password`).
- **Текст / text item** — один .txt файл с HTML-разметкой, который нужно
  опубликовать ровно один раз.
- **Проект** — рабочий контейнер сотрудника; объединяет несколько прогонов и
  свой счётчик использованных админок.
- **Группа / group** — оргструктурная единица (отдел/команда). Админ группы
  видит данные всей своей группы.
- **Роль / role** — набор прав. У пользователя может быть несколько ролей.
- **Страница / page** — путь в админке. Доступ к странице задаётся на уровне
  роли и/или конкретного пользователя.
