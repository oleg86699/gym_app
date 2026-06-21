# Архитектурные решения (ADR)

Здесь зафиксированы ключевые решения, которые влияют на проект в целом. Каждое
решение оформлено как мини-ADR: контекст → решение → последствия.

---

## ADR-001. Две очереди задач: Celery + TaskIQ

**Контекст.** Нагрузка делится на два класса:
1. **Лёгкие async-задачи под UI** — fetch данных по API, валидация одной
   админки, поставить прогон в очередь, scheduled cron для регулярных проверок.
   Это короткие IO-bound задачи (десятки мс — десятки секунд).
2. **Тяжёлые длинные прогоны** — постинг 1000+ текстов на 1000+ сайтов. Это
   часы работы, с десятками параллельных XML-RPC запросов, ретраями, паузами.

**Решение.**
- **TaskIQ + Redis** для класса (1). Async-native, типизированные таски,
  scheduled jobs (`taskiq-aio-redis` или `taskiq-redis` + APScheduler-like
  scheduler).
- **Celery + Redis** для класса (2). Один прогон = одна Celery task. Внутри
  таски используется `asyncio.run(...)` + `httpx.AsyncClient` + семафор на
  N параллельных XML-RPC запросов (25–50). Celery-воркеров запускаем
  немного (1–4), каждый держит один большой прогон. Мониторинг — Flower.

**Граница ответственности.**
| Где                                          | Очередь  |
|----------------------------------------------|----------|
| Валидация одной админки по кнопке            | TaskIQ   |
| Регулярная валидация всех админок (cron)     | TaskIQ   |
| Подсчёт агрегатов на дешборде                | TaskIQ   |
| Email-нотификация                            | TaskIQ   |
| Запуск прогона (старт большой Celery таски)  | TaskIQ → Celery |
| Сам прогон постинга                          | Celery   |

**Последствия.**
- (+) Каждая очередь оптимизирована под свой профиль нагрузки.
- (+) Тяжёлые прогоны не блокируют лёгкие задачи в UI.
- (−) Две системы — два конфига, два набора воркеров, два мониторинга.
- (−) Разработчику нужно понимать, куда что класть. Мы документируем границу
  в этом ADR и в README модуля `workers/`.

Если на практике окажется, что Celery избыточен (TaskIQ + asyncio.gather внутри
одной таски справляется), можем выбросить Celery позже. Заложили возможность.

---

## ADR-002. Хранение .txt файлов: MinIO + метаданные в БД

**Контекст.** Один прогон может содержать 1000+ .txt файлов по ~10 КБ каждый.
В системе одновременно могут идти десятки прогонов. Файлы:
- не редактируются после загрузки (read-only после раскладки в очередь),
- удаляются после успешной публикации (требование ТЗ: «1 текст = 1 успешный
  пост»),
- иногда нужно отдать пользователю (preview), но в основном читаются воркером.

**Альтернативы рассмотрены:**
1. **Содержимое в Postgres (`TEXT` колонка)**. Плюсы: транзакционно, никаких
   orphan-ов. Минусы: раздувает БД, ухудшает performance дешбордов
   (на каждом `SELECT *` тащим мегабайты), бэкапы дольше.
2. **Локальная ФС в Docker volume**. Минусы: нельзя горизонтально масштабировать
   воркеры между серверами, riskуем потерять файлы при переезде volume —
   именно эта проблема была в Zebroid с битыми сейвами.
3. **MinIO (self-hosted S3)** — выбрано.

**Решение.** Файлы хранятся в **MinIO** (запускается контейнером в
docker-compose) с структурой ключей:
```
text-items/{project_id}/{run_id}/{text_item_id}.txt
results/{run_id}/{filename}.csv
```

В БД хранится только метаданные:
```
text_items
├── id (PK)
├── posting_run_id (FK, indexed)
├── project_id (FK, indexed)
├── storage_key (TEXT, путь в MinIO)
├── original_filename
├── status (enum: pending | posting | posted | failed | skipped)
├── title (вытащено из <title>...</title> при загрузке)
├── content_hash (sha256, для дедупа)
├── byte_size
├── posted_url (NULL до публикации)
├── post_id (NULL до публикации)
├── wp_access_id (FK на использованную админку, NULL до публикации)
├── attempts (счётчик)
├── last_error (TEXT, NULL)
├── created_at
├── posted_at (NULL до публикации)
└── deleted_at (NULL — soft delete после успеха для аудита)
```

**Доступ.** Подписанные URL (presigned) MinIO с TTL 5 минут для скачивания
пользователем; воркер обращается напрямую с server-side credentials.

**Бэкап.** MinIO bucket бэкапится отдельно (rclone в S3 / в локальный
NAS — настроить позже).

**Последствия.**
- (+) БД остаётся компактной, дешборды быстрые.
- (+) Стандартный S3 API — легко мигрировать в AWS/GCS если понадобится.
- (+) Никаких file-permission проблем между контейнерами.
- (−) Дополнительный сервис в стеке.
- (−) Двухфазная запись (сначала MinIO, потом DB row) требует обработки
  race condition: если БД упала после загрузки в MinIO — нужен фоновый
  GC orphan-объектов. Решение: загружать сначала в MinIO во временный префикс
  `tmp/{upload_id}/`, потом в одной транзакции БД переносить ссылку и
  переименовывать ключ. TaskIQ-задача раз в час чистит `tmp/` старше 24ч.

---

## ADR-003. Денормализованные счётчики на родительской строке прогона

**Контекст.** ТЗ требует дешборды с десятками-сотнями прогонов одновременно.
Каждый прогон содержит 1000+ text_items. Если на каждый рендер дешборда делать
`COUNT(*) ... WHERE status = 'posted'` по миллионам строк — UI будет тормозить
(точно та же беда, что в Flask-версии: GROUP BY на `children_tasks` каждый
заход на `/projects`).

**Решение.** Денормализованные счётчики на родительских таблицах:
```
posting_runs
├── ...
├── total_texts          (INT, set on upload)
├── posted_count         (INT, default 0)
├── failed_count         (INT, default 0)
├── skipped_count        (INT, default 0)
└── last_progress_at     (TIMESTAMP, для оценки скорости)

projects
├── ...
├── active_runs_count    (INT)
├── total_posted_count   (INT)
└── total_failed_count   (INT)
```

**Как обновляем.** Только в одном месте — в воркере, в той же транзакции, что
и запись результата text_item:
```python
async with session.begin():
    text_item.status = "posted"
    text_item.posted_url = url
    await session.execute(
        update(PostingRun)
        .where(PostingRun.id == run_id)
        .values(posted_count=PostingRun.posted_count + 1)
    )
```

Это `UPDATE ... SET col = col + 1` — атомарно на уровне Postgres, без race
conditions даже при параллельной обработке.

**Когда счётчики могут разойтись с правдой.** Только если воркер упал между
двумя `UPDATE` — на этот случай делаем reconciliation job (TaskIQ, раз в час),
который пересчитывает счётчики по агрегату text_items для активных прогонов.

**Альтернатива (отброшена): триггеры в БД.** Плюс — гарантированная
консистентность. Минус — невидимая магия, тяжело отлаживать, конфликты с
миграциями.

**Альтернатива (отброшена): materialized view.** Имеет смысл когда нужны
сложные join-ы для одного дешборда. Сейчас отдельные счётчики проще.

**Последствия.**
- (+) Все дешборды читают один SELECT без COUNT-ов.
- (+) Реал-тайм апдейты через SSE/WebSocket — отдаём только дельту счётчиков.
- (−) Логика обновления счётчиков должна быть в одном месте (в сервисе
  `posting_service.mark_text_posted`), нельзя апдейтить text_item напрямую
  из других мест. Это enforce-ится через приватность модели и code review.

**Связанная конвенция таблиц.** На **каждой** боевой таблице обязательны:
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` (обновляется через SQLAlchemy
  event listener или `ON UPDATE` триггер — выберем при имплементации).
- `deleted_at TIMESTAMPTZ NULL` для таблиц, где предполагается soft-delete
  (projects, posting_runs, wp_accesses, admin_users).

Это даёт бесплатно: естественный ключ для будущего партиционирования
(`created_at` помесячно), soft-delete и аудит-следы. Все default-ные запросы
из сервисов фильтруют по `deleted_at IS NULL`.

---

## ADR-004. Индексы

Базовый набор индексов на боевых таблицах (создаются в первой миграции этапа 2):

```sql
-- text_items: основной воркхорс
CREATE INDEX ix_text_items_run_status ON text_items (posting_run_id, status);
CREATE INDEX ix_text_items_project_status ON text_items (project_id, status);
CREATE INDEX ix_text_items_wp_access ON text_items (wp_access_id)
    WHERE wp_access_id IS NOT NULL;

-- posting_runs
CREATE INDEX ix_runs_project_status ON posting_runs (project_id, status);
CREATE INDEX ix_runs_status_scheduled ON posting_runs (status, scheduled_for)
    WHERE status IN ('scheduled', 'wait');  -- partial index

-- wp_accesses
CREATE UNIQUE INDEX ux_wp_accesses_domain_login ON wp_accesses (domain, login);
CREATE INDEX ix_wp_accesses_valid ON wp_accesses (is_valid, error_counter)
    WHERE is_valid = true;

-- проектная история использования админок (для счётчика "доступных")
CREATE UNIQUE INDEX ux_project_wp_used ON project_wp_used (project_id, wp_access_id);

-- audit_log
CREATE INDEX ix_audit_user_at ON audit_log (user_id, created_at DESC);
```

`EXPLAIN ANALYZE` на каждой странице админки в стейдже — в обязательном чек-листе
перед мерджем PR, который меняет запросы.

---

## ADR-005. RBAC: гибкая модель из server_app

Берём проверенную модель из server_app:

```
AdminUser
├── id, username, email (опц.), hashed_password, is_active, created_at, last_login
├── group_id → AdminGroup
├── roles ↔ AdminRole (M:M через user_roles)
└── direct_pages ↔ AdminPage (M:M через user_pages — точечные исключения)

AdminGroup
├── id, name, description, is_active
└── users → AdminUser[]

AdminRole
├── id, name, description, is_active
├── users ↔ AdminUser
├── permissions ↔ AdminPermission (M:M)
└── pages ↔ AdminPage (M:M через role_pages)

AdminPermission
├── id, code (e.g. 'users.create'), resource, action
└── roles ↔ AdminRole

AdminPage
├── id, path (/users), name, is_active
├── roles ↔ AdminRole
└── users ↔ AdminUser
```

**Ключевые отличия от server_app.**
1. **`is_super_admin` — не флаг, а роль.** В server_app был булевский флаг на
   юзере. У нас единственный пользователь по умолчанию (`admin`) при первом
   старте получает роль `super_admin` (seed-миграцией). Эта роль обладает
   "звёздочкой" — пермишеном `*` и доступом ко всем страницам. Назначить эту
   роль может только пользователь, у которого она уже есть. Сама роль `*`
   не редактируется и не удаляется.
2. **Иерархия видимости данных** (новое требование):
   - `super_admin` (через роль) → видит всё.
   - `group_admin` (через роль) → видит только своих юзеров группы + их
     проекты/прогоны/админки.
   - `manager` (default для созданных юзеров) → видит только свои объекты.
   Это решается не отдельной табличкой, а **scope-фильтром** на уровне
   data-access слоя: каждый запрос к ресурсам обязан принимать `viewer`
   и фильтровать. Реализуем как mixin `Scoped`.
3. **Новые страницы закрыты по умолчанию для всех, кроме `super_admin`.**
   Это значит: на каждую новую страницу seed-миграцией создаётся запись в
   `admin_pages` без привязок к ролям/юзерам. Middleware `page_access_required`
   пропускает только `super_admin` или тех, кому страница явно открыта.

**Пермишены vs страницы — в чём разница.**
- **Page access** — может ли пользователь попасть на страницу `/users`.
- **Permission** — может ли он внутри страницы выполнить действие, например
  `users.delete`.
- На фронте используем оба: страницы скрываем из меню, кнопки/действия —
  по permission.

---

## ADR-006. Auth: JWT в httpOnly cookie

- Логин выдаёт JWT (HS256, секрет из env), кладёт в httpOnly cookie
  `admin_token` с `SameSite=Lax`, `Secure` (на prod).
- TTL 24 часа; рефреш не делаем сейчас — просто релогин. Если станет
  больно — добавим refresh-токены отдельно.
- Параллельно тот же JWT можно положить в заголовок `Authorization: Bearer` —
  это нужно для developer API (этап 4) и для тестов.
- Logout — очищаем cookie.

**API keys для developer API** (этап 4) — отдельная таблица `api_keys`, хеш
ключа, scope, owner_user_id. Передаются заголовком `X-API-Key`.

---

## ADR-007. Прогресс прогона в real-time

**Решение.** **SSE (Server-Sent Events)**.
- Один endpoint `GET /admin/api/postings/{run_id}/events` — стрим JSON-событий
  `{event: "progress", data: {posted, failed, skipped, total}}`.
- Воркер при апдейте счётчиков пишет событие в Redis pub/sub канал
  `run:{run_id}`. FastAPI endpoint подписан на канал и форвардит в SSE.

**Почему не WebSocket.** Прогресс — однонаправленный канал (server → client),
SSE проще, переподключается автоматически, не требует отдельной авторизации
на upgrade.

---

## ADR-008. Object Storage GC

Возможные orphan-ы:
- Файлы в MinIO без строки в БД (загрузили, БД не доехала)
- Строки text_items со статусом `posted` старше N дней — можно физически
  удалить из MinIO, оставив только метаданные с `storage_key = NULL`

**TaskIQ scheduled jobs:**
- `gc_tmp_uploads` — раз в час, чистит `tmp/{upload_id}/` старше 24ч в MinIO
- `gc_posted_texts` — раз в сутки, удаляет в MinIO файлы text_items со
  статусом `posted` и `posted_at < now() - INTERVAL '30 days'`

---

## ADR-009. Тулинг качества

**На каждый PR (CI):**
- `ruff check` + `ruff format --check`
- `mypy src` (только на touched-файлы первое время, потом расширяем)
- `pytest backend/tests` (unit + integration с testcontainers Postgres/Redis)
- `eslint` + `prettier --check` для UI
- `vitest run` для UI unit-тестов
- Playwright e2e на минимальный happy-path (логин → users page) — не тяжёлый,
  10 секунд

**Pre-commit hooks (локально):**
- ruff, black, prettier, eslint, detect-secrets
- Conventional commit для commit-msg

**Не блокируем merge на:**
- snapshot тесты UI (только предупреждение)
- coverage процент (просто отчёт)

---

## ADR-010. Версии Python и Node

- **Python 3.12** — закреплено в `.python-version` и в Dockerfile
- **Node 22 LTS** — закреплено в `.nvmrc` и в ui.Dockerfile

Зависимости пинаются через `uv.lock` (Python) и `package-lock.json` (npm).
Dependabot включён для security обновлений.

---

## ADR-011. Производительность и план роста

**Контекст.** Целевая нагрузка через 12–18 месяцев эксплуатации:
- ~1 000 завершённых прогонов
- ~1 000 000 строк `text_items`
- ~50 000 строк `wp_accesses`
- ~1 000 000 строк `project_wp_used`
- ~10–20 одновременных активных прогонов
- ~25–50 параллельных XML-RPC запросов на прогон

Это **не big data** для Postgres, но требует базовой инженерной гигиены с
первого дня. Главные риски — bloat от частых UPDATE на `posting_runs`
(счётчики), connection pool exhaustion при параллельных воркерах, и
наивные запросы без индексов/пагинации.

**Решение.** Делим работы на три категории по принципу
«стоимость баков-сейчас vs стоимость баков-потом».

### Категория А — закладываем сразу (этап 1–2)

Дёшево сделать сейчас, дорого добавить потом. Это **не оптимизация**, это
часть фундамента.

| Что | Где в плане | Зачем |
|---|---|---|
| **PgBouncer** перед Postgres в compose, transaction mode | Stage 1: `chore/pgbouncer-and-pg-tuning` | Мультиплексирование коннектов; иначе при 50 воркерах + UI пул соединений Postgres лопается |
| **Cursor-based пагинация** во ВСЕХ list-эндпоинтах (`?cursor=...&limit=...`) | Stage 1: `chore/streaming-and-pagination-conventions` | Стабильна при вставках; быстрее offset на больших таблицах; контракт API нельзя ломать потом |
| **StreamingResponse** для тяжёлых выгрузок (CSV прогона, list text_items) | Stage 1: та же ветка | Не материализуем 1000+ строк в памяти процесса |
| **`created_at` + `updated_at` + `deleted_at`** на каждой боевой таблице | ADR-003 (конвенция); Stage 1 миграции | Soft-delete, аудит, будущий ключ партиционирования |
| **Индексы из ADR-004 в первой миграции** соответствующей таблицы | Stage 2: `feat/db-models-...` | `CREATE INDEX CONCURRENTLY` на полной таблице блокирует записи; в пустой — мгновенно |
| **Денормализованные счётчики на posting_runs/projects** (ADR-003) | Stage 2 | Без этого дешборды лягут при первом росте таблиц |
| **`autovacuum` тюнинг** на горячих таблицах через `ALTER TABLE ... SET (...)` в миграциях | Stage 2 миграция | `posting_runs` получает 1000+ UPDATE на прогон → bloat без агрессивного vacuum |
| **`SELECT FOR UPDATE SKIP LOCKED`** в `pick_wp_access` | Stage 2: `feat/posting-worker-celery` | Корректность «1 админка = 1 проект» при параллельных воркерах |
| **Backpressure**: лимит активных прогонов на пользователя (env `MAX_ACTIVE_RUNS_PER_USER`, default 5) | Stage 2: `feat/run-control-endpoints` | Защита от случайного шторма |
| **Структурированные JSON-логи** (`structlog`) + `trace_id` middleware, который прокидывается в Celery/TaskIQ контекст | Stage 1: `chore/structured-logging` | Без этого диагностика прода — мука |
| **`ANALYZE` после массовых вставок** (распаковка архива) | Stage 2 в worker task | Планировщик иначе ходит по старой статистике |
| **Health-checks на каждый сервис** в compose + `depends_on: condition: service_healthy` | Stage 1: `chore/docker-compose-dev` | Спасает от race conditions при старте |

**Бюджет:** добавляет ~2–3 дня к этапу 1 и ~1 дню к этапу 2 поверх
исходного плана. Окупается с первого прогона.

### Категория Б — готовим почву, активируем по триггеру

Не делаем сейчас, но **выбираем ключевые контракты так**, чтобы будущая
активация была миграцией на день, а не на месяц.

| Что | Подготовка | Триггер активации | Стоимость активации |
|---|---|---|---|
| **Партиционирование `text_items`** по `posting_run_id` (range) | Все hot-path запросы пишем с фильтром по `posting_run_id` всегда. Уникальные индексы делаем составными с `posting_run_id` | `pg_relation_size('text_items') > 5 ГБ`, **или** `ANALYZE` > 5 мин, **или** медиана `pick_text` > 50 мс | 1 день: создать партиционированную таблицу, скопировать партициями, переименовать, переписать FK |
| **Партиционирование `audit_log`** по `created_at` помесячно | `created_at` уже есть. Запросы фильтруют по дате | Размер > 2 ГБ или старый месячный диапазон редко запрашивается | 0.5 дня + pg_partman |
| **Архивная таблица для завершённых прогонов** (`text_items_archive`) + ночная job переноса | Заготовлен скелет миграции и TaskIQ scheduled job, но не активирован (флаг `ENABLE_ARCHIVE=false`) | Hot-таблица > 3 ГБ, либо EXPLAIN дешбордов начал ходить мимо индекса | 1 день: написать job, тестово прогнать, включить флаг |
| **Materialized view** для глобальных дешбордов | Денормализованных счётчиков из ADR-003 хватает. MV закладываем, если конкретный дешборд просел | Запрос дешборда > 500 мс при горячем кеше | 0.5 дня: создать MV, scheduled REFRESH CONCURRENTLY |
| **Read replica Postgres** | `core/db.py` имеет две session-фабрики — `get_db_write`, `get_db_read`. Сейчас обе указывают на primary через один `DATABASE_URL` | CPU primary > 70% в течение часа на чтении | 1 день: поднять streaming replica, добавить `DATABASE_READ_URL` env |
| **Шифрование паролей `wp_accesses` (Fernet)** | Интерфейс `encrypt()/decrypt()` в `core/security.py` с самого начала; до этапа 3 возвращает plaintext | Этап 3, проверка перед прод-деплоем | Data-миграция: зашифровать все существующие пароли разово |
| **Полнотекстовый поиск по доменам/именам** через GIN + pg_trgm | Закладываем `pg_trgm` extension в init-миграции, не используем | Жалобы пользователей на медленный поиск в WP-админках | 0.5 дня: добавить GIN индекс, переключить `ILIKE` на `% similarity` |
| **Redis-кеш для тяжёлых read-only запросов** | Все агрегаты идут через сервис-слой; кеш-обёртка добавляется одним декоратором | Конкретный endpoint медленный на горячих данных | 0.5 дня на конкретный endpoint |

**Бюджет:** ~0 дополнительного времени сейчас (только дисциплина при
написании запросов и схемы). Активация — каждое отдельно по дню.

### Категория В — сознательно НЕ делаем

Преждевременная сложность, которая снизит скорость разработки и поднимет
порог входа для новых членов команды. Включаем **только** при доказанной
необходимости.

- **Sharding / multi-tenant DB.** Налог: распределённые транзакции,
  никаких FK между шардами. Постгрес single-instance тянет десятки
  миллионов строк без проблем.
- **Очередь не-Redis (Kafka/RabbitMQ).** Redis на наши объёмы избыточен,
  не недостаточен.
- **Микросервисы.** Один FastAPI app + воркеры — правильный масштаб
  при размере команды до 10 инженеров.
- **GraphQL / gRPC / CQRS.** REST + OpenAPI закроет всё, что нужно.
- **Elasticsearch / Meilisearch.** Postgres + pg_trgm покроет поиск.
- **Микробенчмарки и premature profiling.** Профилируем по факту, не
  заранее.

### Чек-лист перформанс-ревью перед мерджем PR

Любой PR, добавляющий новый запрос или эндпоинт, должен пройти:

1. На horror-стенде (БД с 1М text_items, генерим из фейк-генератора)
   `EXPLAIN (ANALYZE, BUFFERS)` для нового запроса показывает:
   - `Index Scan` или `Index Only Scan` (не `Seq Scan`) на больших таблицах
   - `cost` верхнего узла < 10 000 для list-эндпоинтов
   - `actual time` < 100 мс для list-эндпоинтов
2. List-эндпоинт принимает `cursor` и `limit`.
3. Тяжёлые экспорты возвращают `StreamingResponse`.
4. Нет `await session.execute(...)` в цикле — массовые операции через
   `bulk_insert_mappings` или `execute(insert(...).values([...]))`.

Чек-лист — в `CONTRIBUTING.md`, проверяется ревьюером.

### Мониторинг триггеров активации

В этапе 3 (Prometheus + Grafana) — алерты:
- `pg_relation_size('text_items')` каждые 6 часов
- Медиана latency главных list-эндпоинтов
- CPU и connection count Postgres
- Глубина очереди Redis

При срабатывании алерта — открываем соответствующий пункт категории Б и
включаем его.

---

## ADR-012. Бэкапы

**Контекст.** Что мы боимся потерять:
- **Postgres** — всё состояние приложения (users, projects, runs, text_items
  метаданные, RBAC). Без БД нет ничего.
- **MinIO** — оригинальный контент text-items и результирующие CSV. Без
  них прогон не воспроизвести, но приложение всё ещё работает.
- **Redis** — кэш и очереди. Безвозвратно — задачи в очереди потеряются,
  но воркер их подберёт повторно из БД (см. ADR-001 «состояние в Postgres»).
  **Не бэкапим.**
- **Код, конфиги, миграции** — git. Не бэкапим отдельно.

**Решение.**

### Что и куда

Отдельный сервис `backup` в docker-compose:
- Базовый образ: `alpine` + `postgresql-client` + `mc` (MinIO client) + `cron`.
- Раз в **48 часов** в 03:30 Europe/Kyiv (cron: `30 3 */2 * *`) запускает
  скрипт `deploy/backup/run.sh`.
- Хостовая папка `./backups/` смонтирована как `/backups` внутрь контейнера.

Скрипт делает:
1. `pg_dump -Fc` (custom format, сжатый) →
   `./backups/postgres/{YYYY-MM-DD}_{hhmm}.dump`
2. `mc mirror --overwrite --remove minio/text-items /backups/minio/text-items/`
   (инкрементальный — копирует только изменённое; `--remove` чистит то,
   чего уже нет в источнике)
3. То же для bucket `results`
4. Скрипт ротации: удаляет всё в `./backups/postgres/` старше 7 дней
   (по mtime). MinIO-зеркало само эквивалент текущего состояния — отдельная
   ротация не нужна, но при желании можно держать снимки через `--versioning`.
5. Пишет лог в `./backups/log/{YYYY-MM-DD}.log` + строку «OK/FAIL» в
   syslog контейнера (увидим в `docker compose logs backup`).

### Расположение бэкапов

- **Прототип / dev**: `./backups/` рядом с docker-compose файлом.
- **Сервер**: та же `./backups/` относительно `/opt/<repo>/`. Хостовая
  папка должна быть на отдельном диске или хотя бы в другой точке
  монтирования от `postgres_data`/`minio_data` (на случай отказа диска).
  Это требование документируется в README сервера.

### Восстановление (runbook)

Полный текст — `deploy/backup/RESTORE.md`. TL;DR:
```bash
# Postgres
docker compose stop app celery-worker taskiq-worker
docker compose exec db dropdb -U $POSTGRES_USER $POSTGRES_DB
docker compose exec db createdb -U $POSTGRES_USER $POSTGRES_DB
cat ./backups/postgres/2026-05-15_0330.dump | \
  docker compose exec -T db pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --no-owner
docker compose start app celery-worker taskiq-worker

# MinIO
docker compose exec backup mc mirror --overwrite /backups/minio/text-items/ minio/text-items
docker compose exec backup mc mirror --overwrite /backups/minio/results/ minio/results
```

Восстановление проверяется **раз в месяц** — отдельный compose-стек
поднимается на чистой папке, накатывается последний бэкап, прокликивается
smoke-сценарий. Дата последней удачной проверки фиксируется в
`deploy/backup/last_restore_check.md`.

### Зашифрованность

Сейчас бэкапы лежат на хосте в открытом виде. Доступ к серверу = доступ к
бэкапам — это эквивалентно доступу к живой БД, не хуже. Когда появится
off-site (категория Б ниже) — там шифруем `gpg --symmetric` ключом из
`SOPS`/секрет-менеджера.

### Категория Б — off-site бэкапы (потом)

Закладываем интерфейс: переменная `OFFSITE_BACKUP_TARGET` в `.env`,
поддерживаемые значения `s3://...`, `b2://...`, `rclone:remote:path`.
Сейчас пустая → шаг пропускается. Когда заведём прод — заполняем, скрипт
после локального дампа делает `rclone copy` на удалённый storage и шифрует
GPG-ключом из env.

**Последствия.**
- (+) Один контейнер, никакой магии хостовых cron-ов.
- (+) Прототип, dev и prod работают по одной схеме — только разные пути
  монтирования.
- (+) MinIO mirror инкрементальный — экономит место и время.
- (−) Постгрес-бэкап полный каждый раз (для нашего объёма ~1 ГБ это
  ОК; для больших — добавим WAL-G/pgBackRest когда понадобится).
- (−) Бэкап на тот же хост — защищает от логических ошибок (DROP TABLE),
  но не от отказа диска. Реальная защита диска — только off-site
  (категория Б).

---

## ADR-013. Управление дисковым пространством и docker-образами

**Контекст.** На dev/prod-серверах место кончается из-за:
1. Каждый деплой создаёт новый image-тег (`v1.2.3`, `v1.2.4`, …) — старые
   образы висят бесконечно, пока кто-то не почистит.
2. Build cache buildkit-а растёт слоями.
3. Docker daemon-логи пишутся без ротации, на болтливом контейнере
   `/var/lib/docker/containers/<id>/<id>-json.log` распухает до гигабайт.
4. Dangling-образы после rebuild (старый тег теперь без имени).

Это и есть «забивает память на небольших серверах», про которое говорил
пользователь. Лечится дисциплиной деплоя + регулярной чисткой.

**Решение.**

### Ротация docker-логов (применяется к ВСЕМ контейнерам сразу)

Хостовый файл `/etc/docker/daemon.json` (документируется в README сервера
+ ansible-роль когда подъедет деплой):

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

После правки — `systemctl restart docker`. Раз настроил — забыл.

### Политика тегов и пины

- Наши образы тегируем строго `{version}` (`v1.2.3`) + плавающий
  (`dev-latest` / `latest`).
- В compose ВСЕГДА указываем конкретную версию через env var,
  никогда `:latest` без явного pin. Это даёт воспроизводимость + позволяет
  отличать «нужный» образ от «забытого».

### Чистка после деплоя

В deploy workflow (когда подключим git/CI) и как scheduled job на хосте:

```bash
# Удалить dangling и неиспользуемые > 7 дней
docker image prune -af --filter "until=168h"

# Builder cache > 7 дней
docker builder prune -af --filter "until=168h"

# Остановленные контейнеры
docker container prune -f --filter "until=168h"

# Локальные неиспользуемые сети
docker network prune -f --filter "until=168h"
```

**Volumes мы НИКОГДА не пруним автоматически** — `docker volume prune`
в скрипт не попадает. Если в проекте появятся именованные volume-ы (см.
ADR-014, мы их избегаем), их чистка — ручная, осознанная.

### Где это живёт

- Скрипт `deploy/maintenance/prune.sh` — реализация выше.
- В backup-контейнере (ADR-012) тот же cron-демон, что и для бэкапа,
  запускает `prune.sh` раз в неделю: `0 4 * * 0` (воскресенье 04:00).
- В `Makefile` есть target `make prune` для ручного запуска локально.

### Disk-space мониторинг

В этапе 3 (Prometheus + node_exporter) — алерт при `disk_used > 80%`.
До этого — никаких автоалертов, на прототипе и так заметно.

### Лимит размера образов

В CI (этап 1.5, когда подключим) добавляем шаг:
```yaml
- name: Check image size
  run: |
    size=$(docker image inspect $IMAGE --format='{{.Size}}')
    if [ "$size" -gt $((600 * 1024 * 1024)) ]; then
      echo "::warning::Image $IMAGE is $(($size / 1024 / 1024))MB — над лимитом 600MB"
    fi
```

Бэкенд-образ должен умещаться в ~500 МБ (Python + uv + asyncpg + httpx +
celery — реалистично), UI prod-образ — < 50 МБ (nginx + статика).

**Последствия.**
- (+) Один раз настроил `daemon.json` — логи под контролем навсегда.
- (+) Чистка раз в неделю + после каждого деплоя — место не утекает.
- (+) Volumes защищены от случайного prune.
- (−) Если деплой случился с багом и старый образ ушёл — за 7 дней он
  ещё доступен для отката (`docker run ... v1.2.2`). После — только
  pull заново из GHCR.

---

## ADR-014. Персистентность данных: bind mounts, не теряем файлы

**Контекст.** В прошлом проекте теряли файлы, потому что папка с данными
жила внутри контейнера или во временной точке, которая затиралась при
деплое. Главная причина — два классических антипаттерна:

1. **Данные внутри слоя образа** (нет volume вообще). `docker compose pull`
   = новый образ = старый container уничтожен = данные нет.
2. **Named volume + случайный `docker compose down -v`**. Флаг `-v`
   убивает все named volumes; команду легко набрать в спешке.
3. **`bind mount` на ephemeral tmp-папку** (`/tmp`, чужой workspace).

**Решение.**

### Правило 0. Всё, что нельзя терять — через bind mount на хост

Не используем named volumes для боевых данных. Используем bind mount в
явно зарегистрированную хостовую папку рядом с проектом.

### Реестр персистентных путей

Один файл `deploy/PERSISTENT_PATHS.md` — единственный источник правды о
том, что не должно потеряться:

| Хостовый путь | Контейнер | Что лежит | Куда уйдёт при удалении |
|---|---|---|---|
| `./data/postgres/` | `db:/var/lib/postgresql/data` | Вся БД (RBAC, проекты, прогоны, тексты-метаданные) | Полная потеря |
| `./data/minio/` | `minio:/data` | Все .txt контента, CSV-результаты прогонов | Потеря контента, можно частично восстановить из бэкапа |
| `./data/redis/` | `redis:/data` | AOF — задачи в очереди, кэш | Подобрать из БД, потеря только in-flight задач |
| `./backups/` | `backup:/backups` | Бэкапы Postgres + MinIO mirror | Потеря резерва — критично! |
| `./logs/` | `app:/var/log/app` (опц.) | Application JSON logs | Не критично, логи также в docker daemon |

Все эти пути:
- В `.gitignore` (полный путь `data/`, `backups/`, `logs/`).
- В `deploy/PERSISTENT_PATHS.md` с описанием выше.
- Перед первым стартом создаются скриптом `deploy/init-host.sh` с правильными
  правами (`mkdir -p ./data/postgres && chown -R 999:999 ./data/postgres`
  для postgres-юзера в alpine-образе).

### Правило 1. Никогда `docker compose down -v`

В `Makefile` нет target-а с `-v`. Документируется в README сервера.
Хочешь снести всё включая данные — пиши `rm -rf ./data/ ./backups/`
явно после `down`. Это намеренный ритуал, нельзя случайно.

Для удобства локалки — `make reset-db` явно дропает БД через
`docker compose exec db dropdb && createdb`, без удаления `./data/postgres`.

### Правило 2. Deploy scripts не трогают папки данных

В deploy workflow и в `deploy/maintenance/prune.sh` есть жёсткий guard:

```bash
set -euo pipefail
test -d ./data/postgres || { echo "FATAL: ./data/postgres missing!" >&2; exit 1; }
# ... дальше любые prune-ы
```

Если по какой-то причине папки нет — деплой падает, **не идёт** дальше
поднимать новый контейнер на пустом месте.

### Правило 3. Backup-контейнер монтирует данные read-only

```yaml
backup:
  volumes:
    - ./data/postgres:/var/lib/postgresql/data:ro   # ro!
    - ./data/minio:/data/minio:ro
    - ./backups:/backups                            # rw только сюда
```

`pg_dump` идёт через сетевое соединение к контейнеру `db` (не через диск),
а вот mc mirror читает напрямую из ro-mount — безопасно.

### Правило 4. PR-чек на изменения compose

В `CONTRIBUTING.md` (этап 1.5 когда подключим git):
> Если PR меняет `docker-compose*.yaml`:
> — Не появилось ли новых named volumes (`volumes:` блок без `./` префикса)?
> — Не удалены ли существующие bind mounts?
> — Не добавлен ли `down -v` в скрипты?
> Если что-то из этого — явное обоснование в описании PR.

CI (когда подключим) — добавим `bash deploy/maintenance/compose-lint.sh`
который грепает на эти антипаттерны.

### Структура папки проекта на сервере

```
/opt/<repo>/
├── docker-compose.yaml
├── .env
├── data/                  ← bind mounts, в git нет, бэкапятся
│   ├── postgres/
│   ├── minio/
│   └── redis/
├── backups/               ← результаты бэкапов, в git нет
└── deploy/
    ├── PERSISTENT_PATHS.md
    ├── init-host.sh
    └── maintenance/
        ├── prune.sh
        └── compose-lint.sh
```

То же самое локально в `/Volumes/profile/github/gym/app/` (на прототипе
без `/opt/<repo>/` обёртки).

### Что хранится где (резюме)

| Данные | Где | Защита |
|---|---|---|
| Контент text-items (.txt) | MinIO → `./data/minio/` (bind) | Бэкап mc mirror в `./backups/minio/` |
| Метаданные text-items | Postgres → `./data/postgres/` | Бэкап pg_dump в `./backups/postgres/` |
| CSV-результаты прогонов | MinIO → `./data/minio/` | Бэкап mc mirror |
| Бэкапы | `./backups/` (bind) | (TODO) off-site rclone — категория Б |
| Очередь задач Redis | `./data/redis/` AOF | Подбирается из БД (см. ADR-001) |
| Логи приложения | docker json-file logs, ротация 10m×3 | Не бэкапим, опционально → loki в этапе 3 |
| Загруженный архив (.zip) пользователя | MinIO `posting_runs.source_archive_storage_key` | Бэкап mc mirror |

**Последствия.**
- (+) Файлы видны на хосте — `ls ./data/minio/` показывает реальные
  объекты. Удобно для дебага.
- (+) Бэкапы прозрачные: что в `./backups/` — то и восстановим.
- (+) Случайный `docker compose down -v` ничего не сломает, потому что
  bind mounts не пруняются.
- (−) Бэкенд-контейнер должен иметь те же UID/GID, что владеет файлами
  на хосте, иначе permission denied. Решается `user: "999:999"` в
  compose + init-скриптом.
- (−) Bind mounts чуть медленнее named volumes на macOS Docker (известная
  особенность). На Linux разницы нет. На прототипе локально — терпимо.
