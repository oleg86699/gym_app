# Этап 2. Ядро постинга: проекты, прогоны, тексты, админки WP, XML-RPC

## Цель этапа

Закрыть базовое требование ТЗ — массовый постинг работает end-to-end:
- Пользователь создаёт проект.
- Загружает в проект архив `.txt` файлов с HTML-разметкой.
- Загружает CSV с админками WordPress в общий пул (если их там ещё нет).
- Запускает прогон с автодатой и/или отложенным стартом.
- Видит прогресс прогона на странице (без real-time SSE — это в этапе 3,
  здесь только polling).
- По завершении скачивает CSV-результат: ссылки + post_id + домены.
- В рамках одного проекта одна админка используется один раз; админка с
  ошибкой исключается; неуспешный текст остаётся в очереди.
- Кнопка «сбросить историю админок проекта» — для повторного прогона.

## Что входит

1. MinIO в docker-compose + клиент-обёртка
2. Доменные модели: Project, PostingRun, TextItem, WpAccess,
   ProjectWpUsed, RunArtifact
3. Загрузка `.zip` с текстами → парс → создание TextItem + закладка
   контента в MinIO
4. Загрузка CSV админок → дедуп → запись в пул
5. Конфигурация прогона: name, period_days (для автодаты), scheduled_for
   (для отложенного), concurrency, timeout_seconds
6. Воркер постинга: Celery task + asyncio + httpx + семафор на
   concurrency
7. Денормализованные счётчики на PostingRun и Project (см.
   `architecture_decisions.md` ADR-003)
8. Финализация прогона: статус done/error/need_more_admins/cancelled
9. UI: страницы Projects, Project Detail, Posting Run, WP Accesses
10. Скачивание CSV с результатами прогона
11. Кнопка «сбросить историю» (очистка ProjectWpUsed для проекта)

## Что НЕ входит (этап 3+)

- Валидатор админок (валидность определяем «по факту» по ошибкам постинга)
- Real-time прогресс (SSE) — пока polling каждые 5 сек
- Прокси
- Глобальные дешборды очередей и доступов
- Нотификации
- HTML preview (визуальный редактор)

---

## 1. Доменные модели

Все таблицы наследуют `TimestampedMixin` (`created_at`, `updated_at`).
Hot-таблицы с soft-delete-семантикой — `SoftDeletableMixin` (+ `deleted_at`).
См. ADR-003.

```
projects                                                [SoftDeletable]
  id, name, owner_id (FK users), group_id (FK groups, наследуется от owner),
  status (enum: active | archived),
  -- денормализованные счётчики (ADR-003):
  active_runs_count (INT default 0),
  total_posted_count (INT default 0),
  total_failed_count (INT default 0)
  -- created_at, updated_at, deleted_at от mixin-а

posting_runs                                            [SoftDeletable]
  id, project_id (FK), created_by (FK users),
  status (enum: draft | scheduled | queued | running | paused | done
                | failed | need_more_admins | cancelled | interrupted),
  scheduled_for (TIMESTAMPTZ, nullable),
  period_days (INT, default 45),       -- диапазон автодаты в днях назад
  concurrency (INT, default 25),       -- сколько параллельных XML-RPC
  timeout_seconds (INT, default 30),   -- per-request таймаут
  pause_requested (BOOL default false),
  cancel_requested (BOOL default false),
  total_texts (INT),
  posted_count, failed_count, skipped_count (INT, default 0),
  last_progress_at (TIMESTAMPTZ, nullable),
  started_at, finished_at (nullable),
  worker_heartbeat_at (TIMESTAMPTZ, nullable),  -- для recovery после краша
  celery_task_id (TEXT, nullable),
  proxy_id (FK proxies, nullable),               -- введём в этапе 3
  source_archive_storage_key (TEXT)
  -- created_at, updated_at, deleted_at от mixin-а

text_items                                              [Timestamped]
  id, posting_run_id (FK), project_id (FK),
  storage_key (TEXT),                   -- ключ в MinIO
  original_filename, title,
  content_hash (CHAR(64)),
  byte_size (INT),
  status (enum: pending | posting | posted | failed | skipped | reuse_blocked),
  posted_url (TEXT), post_id (BIGINT), wp_access_id (FK wp_accesses),
  posted_at, attempts (INT default 0), last_error (TEXT)
  -- created_at, updated_at от mixin-а
  -- НЕТ deleted_at — text_items живут до архивации (ADR-011, категория Б)

wp_accesses                                             [SoftDeletable]
  id, domain (TEXT), login (TEXT), password (TEXT),
  is_valid (BOOL default true), error_counter (INT default 0),
  language (VARCHAR(10), nullable, выставляется валидатором этапа 3),
  amount_use (INT default 0),
  source_filename (TEXT),
  tag (TEXT, nullable),
  last_validated_at (TIMESTAMPTZ, nullable)
  -- created_at, updated_at, deleted_at от mixin-а

project_wp_used                                         [Timestamped]
  project_id (FK), wp_access_id (FK),
  posting_run_id (FK), text_item_id (FK, для аудита),
  used_at TIMESTAMPTZ
  -- UNIQUE (project_id, wp_access_id) — это и есть «1 админка = 1 проект»

run_artifacts                                           [Timestamped]
  id, posting_run_id (FK),
  kind (enum: source_archive | result_csv),
  storage_key, byte_size
```

**Autovacuum-тюнинг на горячих таблицах** (в той же миграции, что создаёт
таблицы):

```sql
ALTER TABLE posting_runs SET (
  autovacuum_vacuum_scale_factor = 0.02,   -- агрессивнее, каждые 2% мёртвых
  autovacuum_analyze_scale_factor = 0.01,
  autovacuum_vacuum_cost_delay = 10
);
ALTER TABLE text_items SET (
  autovacuum_vacuum_scale_factor = 0.05,
  autovacuum_analyze_scale_factor = 0.02,
  fillfactor = 80                          -- HOT updates без index rewrite
);
ALTER TABLE project_wp_used SET (
  autovacuum_vacuum_scale_factor = 0.1
);
```

**Шифрование паролей админок.** На этапе 2 пока храним как plaintext в БД
с пометкой "TODO: шифровать" — в этапе 3 включим Fernet (ключ из env).

**Где хранится связь run → user.** Через `created_by` в `posting_runs`.
Доступ к чужим прогонам — через scope-фильтры из этапа 1 (super_admin
видит всё, group_admin — свою группу, manager — только свои).

---

## 2. Хранилище MinIO

### 2.1 Compose

Добавить сервис:
```yaml
minio:
  image: minio/minio:latest
  command: server /data --console-address ":9001"
  environment: { MINIO_ROOT_USER, MINIO_ROOT_PASSWORD }
  volumes: [minio_data:/data]
  expose: ["9000", "9001"]
  healthcheck: mc ready
```

В dev override открыть `9000` и `9001` на хост (для UI).

### 2.2 Клиент

`backend/src/core/storage.py` — обёртка над `aioboto3` или `minio` SDK:
```python
class ObjectStore:
    async def upload(key, data: bytes, content_type) -> None
    async def download(key) -> bytes
    async def presigned_url(key, ttl) -> str
    async def delete(key) -> None
    async def list(prefix) -> AsyncIterator[ObjectInfo]
    async def copy(src_key, dst_key) -> None
```

Конкретный бакет вычисляется из ключа (`text-items`, `results`).

### 2.3 GC задачи

См. `architecture_decisions.md` ADR-008 — добавить TaskIQ scheduled
`gc_tmp_uploads` (раз в час).

---

## 3. Загрузка текстов

### 3.1 Endpoint

`POST /admin/api/projects/{project_id}/postings`
multipart-form:
- `name` (str)
- `archive` (file, .zip)
- `period_days` (int, default 45)
- `concurrency` (int, default 25)
- `scheduled_for` (ISO datetime, optional)

### 3.2 Логика

1. **Backpressure**: проверить `MAX_ACTIVE_RUNS_PER_USER` (env, default 5);
   если у пользователя уже столько прогонов в статусах `queued|running|paused` —
   вернуть 429.
2. Создать `posting_runs` со статусом `draft`.
3. Сохранить оригинальный архив в MinIO `tmp/{upload_id}.zip`.
4. Запустить TaskIQ task `unpack_archive(run_id, storage_key)`:
   - распаковать в память,
   - для каждого `.txt`:
     - извлечь `<title>...</title>` и сам контент,
     - посчитать sha256 контента,
     - залить в MinIO `text-items/{project_id}/{run_id}/{uuid}.txt`,
     - собрать в батч 500 строк, `bulk_insert_mappings` в `text_items`
       (один INSERT вместо тысячи),
   - финализировать `posting_runs.total_texts`, перенести архив в
     `posting_runs.source_archive_storage_key`,
   - **`ANALYZE text_items`** — освежить статистику планировщика после
     массовой вставки (ADR-011, категория А),
   - перевести статус run в `scheduled` (если есть `scheduled_for`)
     или сразу в `queued`.
5. Endpoint отвечает синхронно с `run_id` и статусом `unpacking`;
   фронт поллит детальную страницу.

### 3.3 Валидация

- Только `.zip`, mime + magic bytes.
- Лимит по размеру архива (5 ГБ) — настраиваемый из env.
- В архиве не больше 10 000 файлов (защита от zip-bombs). Каждый — `.txt`,
  размер до 1 МБ.

---

## 4. Загрузка админок

### 4.1 Endpoint

`POST /admin/api/wp-accesses/import` (`super_admin` или `group_admin`)
multipart:
- `file` (.csv с колонками `domain,login,password`)
- `tag` (str, optional)

### 4.2 Логика

- Парсим CSV (требуем заголовок).
- Делаем `INSERT ... ON CONFLICT (domain, login) DO NOTHING`.
- Возвращаем `{imported, skipped_duplicates}`.

### 4.3 Endpoint списка

`GET /admin/api/wp-accesses` — пагинация, фильтр по `tag`, `is_valid`,
search по `domain`.
`DELETE /admin/api/wp-accesses/{id}` (super_admin).

---

## 5. Воркер постинга

### 5.1 Celery task

`backend/src/workers/celery/posting.py`:
```python
@celery_app.task(bind=True, name="run_posting")
def run_posting(self, run_id: int) -> None:
    asyncio.run(_run_posting_async(run_id, self.request.id))
```

### 5.2 `_run_posting_async`

```python
async def _run_posting_async(run_id, task_id):
    async with AsyncSession() as s:
        run = await s.get(PostingRun, run_id)
        run.celery_task_id = task_id
        run.status = "running"
        run.started_at = now()
        await s.commit()

    sem = asyncio.Semaphore(run.concurrency)
    async with httpx.AsyncClient(
        timeout=run.timeout_seconds,
        proxy=run.proxy,  # для этапа 3
        verify=False,
    ) as client:
        async def process(text_item_id):
            async with sem:
                await post_one_text(client, run_id, text_item_id)

        # Берём текстовки чанками, чтобы не материализовать миллион в памяти
        while True:
            batch = await fetch_pending_texts(run_id, limit=100)
            if not batch:
                break
            await asyncio.gather(*(process(t.id) for t in batch),
                                 return_exceptions=True)
            # проверяем pause/cancel
            if await is_run_paused_or_cancelled(run_id):
                break

    await finalize_run(run_id)
```

### 5.3 `post_one_text`

```python
async def post_one_text(client, run_id, text_id):
    # Берём первую свободную WpAccess: is_valid и НЕ в project_wp_used для project
    while True:
        access = await pick_wp_access(run_id)
        if access is None:
            await mark_run_need_more_admins(run_id)
            return
        result = await try_xmlrpc_post(client, access, text_id)
        if result.success:
            await record_success(run_id, text_id, access, result)
            return
        if result.is_auth_error or result.is_4xx:
            await record_admin_invalid(access, reason=result.reason)
            # пробуем следующую админку
            continue
        if result.is_timeout:
            await record_admin_skipped_temporarily(access)
            continue
        await record_text_failed(run_id, text_id, reason=result.reason)
        return
```

### 5.4 `pick_wp_access`

SQL с `SELECT ... FOR UPDATE SKIP LOCKED` — чтобы параллельные воркеры
гарантированно не брали одну и ту же админку дважды:
```sql
SELECT a.id FROM wp_accesses a
WHERE a.is_valid = true
  AND NOT EXISTS (
    SELECT 1 FROM project_wp_used pu
    WHERE pu.project_id = :project_id AND pu.wp_access_id = a.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM in_flight_wp_locks l
    WHERE l.wp_access_id = a.id
  )
ORDER BY random()
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

`in_flight_wp_locks` — короткоживущая таблица для блокировки админки на
время её попытки в текущем прогоне (alternative — Redis SETNX). Решим
при имплементации; SKIP LOCKED проще.

### 5.5 XML-RPC клиент

`backend/src/infrastructure/wp_client/xmlrpc.py`:
- Самописный XML методом POST через httpx (без `python-wordpress-xmlrpc`,
  она не async).
- Сначала пробуем `wp.newPost` с published-status и `post_date` из
  `now() - random(0, period_days)`.
- Если 401/403 — `account_invalid`.
- Если 5xx — `server_error` (retry на другой админке).
- Если timeout — `timeout`.
- Если 200 и ответ содержит `post_id` — успех. Возвращаем URL: пробуем
  `?p={post_id}` или парсим pretty-url из ответа (как Flask-версия с
  follow-redirects).

Для пермалинков — после получения `post_id` делаем GET на
`{domain}/?p={post_id}` с `follow_redirects=True` и берём финальный URL.

### 5.6 Денормализованные счётчики

При `record_success` и `record_text_failed` в одной транзакции:
```sql
UPDATE posting_runs
SET posted_count = posted_count + 1, last_progress_at = now()
WHERE id = :run_id;
```

---

## 6. Управление прогоном

### 6.1 Endpoints

- `GET /admin/api/projects/{pid}/postings` — список прогонов проекта.
- `GET /admin/api/postings/{run_id}` — детали (включая счётчики).
- `GET /admin/api/postings/{run_id}/text-items` — список текстов
  прогона с пагинацией и фильтрами по статусу.
- `POST /admin/api/postings/{run_id}/start` — поставить в очередь Celery.
  Если `scheduled_for` в будущем, ставим TaskIQ scheduled job, который
  через scheduled_for поставит Celery task.
- `POST /admin/api/postings/{run_id}/pause` — выставить флаг
  `posting_runs.pause_requested=true`. Воркер увидит и остановится.
- `POST /admin/api/postings/{run_id}/resume`.
- `POST /admin/api/postings/{run_id}/cancel` — graceful, после текущей
  итерации.
- `DELETE /admin/api/postings/{run_id}` — soft-delete (status=archived).
- `GET /admin/api/postings/{run_id}/result.csv` — скачать результаты
  (генерится на лету из `text_items`).
- `POST /admin/api/projects/{pid}/reset-admin-usage` — удаляет все
  `project_wp_used` для проекта.

### 6.2 Финализация

В `finalize_run`:
- Если все text_items в финальных статусах — `status=done`.
- Если есть failed без доступных админок — `status=need_more_admins`.
- Сгенерировать `results/{run_id}.csv` и сохранить в MinIO. Записать в
  `run_artifacts`. Генерация — **через StreamingResponse-стиль курсор по
  БД**, не материализуем все строки в память (ADR-011).
- TaskIQ task `send_run_finished_notification` (для этапа 3 — пока
  заглушка).

### 6.3 Recovery после краша воркера

Воркер каждые 30 сек обновляет `posting_runs.worker_heartbeat_at`.

TaskIQ scheduled job `recover_interrupted_runs` (раз в минуту):

```sql
-- Прогоны, которые в running, но heartbeat молчит больше 2 минут — мертвы
SELECT id FROM posting_runs
WHERE status = 'running' AND
      worker_heartbeat_at < now() - INTERVAL '2 minutes';
```

Для каждого такого:
1. Перевести статус в `interrupted`.
2. Все его `text_items` со статусом `posting` (начали, но не закончили) →
   обратно в `pending`.
3. Запостить событие в Redis канал `run:{id}` чтобы UI понимал.
4. Отправить в-нотификацию владельцу.

Возобновление — кнопка «Resume» в UI, которая ставит статус `queued` и
заново enqueue-ит Celery task. Тот же `SELECT WHERE status='pending'`
подхватит работу с того места, где остановились (см. ADR-001
«состояние в Postgres, а не в памяти»).

---

## 7. UI

### 7.1 Новые страницы

- `/projects` — список проектов пользователя (свои или своей группы).
  Кнопка «новый проект» — модалка с именем.
- `/projects/[id]` — детальный вид:
  - Сводка: имя, владелец, кол-во прогонов, общее посчитано/опубликовано.
  - Большая кнопка «Запустить новый прогон» — модалка с upload и настройками.
  - Кнопка «Сбросить историю админок».
  - Кнопка «Доступные админки сейчас: N» — переходит на отфильтрованную
    страницу `/wp-accesses?available_for_project=N`.
  - Таблица прогонов с прогресс-барами (polling 5 сек).
- `/projects/[id]/runs/[run_id]` — детали прогона:
  - Текущий статус, счётчики, прогресс-бар.
  - Кнопки start/pause/resume/cancel.
  - Таблица text_items (paginated) — name, status, posted_url, used domain.
  - Кнопка «Скачать CSV».
- `/wp-accesses` — таблица всех админок (для super_admin / group_admin):
  - Фильтры: tag, is_valid, доступная для проекта X (опционально).
  - Импорт CSV — модалка.
  - Удаление по одной / bulk.

### 7.2 Скрытие за page-access

Все новые маршруты добавляются seed-миграцией в `admin_pages`. По
умолчанию открыты только для `super_admin` и `manager` (для своих
проектов). Видимость менеджера на странице фильтруется на бэке через
scope.

### 7.3 Polling прогресса

На странице прогона — каждые 5 сек `GET /admin/api/postings/{id}`,
обновление прогресс-бара. SSE/WebSocket — этап 3.

---

## 8. Тесты этапа 2

- **Unit**: парсинг архива, извлечение title, выборка следующей админки
  (race condition тест с SKIP LOCKED).
- **Integration**:
  - Загрузка реального тестового zip (можно взять
    `/Volumes/profile/github/gym/flask/example_data_to_upload/test_text.zip`).
  - Полный прогон на mock-WP (поднять WordPress в test-контейнере или
    замокать httpx через respx).
  - Проверка: счётчики обновлены, CSV содержит правильные URL,
    `project_wp_used` заполнен.
- **E2E (Playwright)**:
  - Загрузить архив → запустить прогон → дождаться завершения (с
    замоканным WP) → скачать CSV.

---

## 9. Декомпозиция на feat-ветки

1. `feat/minio-and-storage-wrapper`
2. `feat/db-models-projects-runs-texts-wp` — со всеми mixin-ами,
   autovacuum-настройками, индексами из ADR-004
3. `feat/wp-accesses-import-api`
4. `feat/projects-crud-api`
5. `feat/upload-archive-and-unpack-task` — bulk insert, ANALYZE, backpressure
6. `feat/xmlrpc-client`
7. `feat/posting-worker-celery` — с heartbeat и SKIP LOCKED
8. `feat/run-recovery-job`
9. `feat/run-control-endpoints` — start/pause/resume/cancel
10. `feat/run-csv-result-streaming`
11. `feat/ui-projects-page`
12. `feat/ui-project-detail-and-run-form`
13. `feat/ui-run-detail-with-polling`
14. `feat/ui-wp-accesses-page`
15. `test/e2e-posting-happy-path`
16. `feat/reset-admin-usage-button`

---

## 10. Критерии приёмки этапа

1. На dev-сервере: пользователь логинится, создаёт проект, загружает
   `example_data_to_upload/test_text.zip` (11 текстов), загружает CSV с
   2-3 рабочими WP-админками (можно тестовые, проверенные руками).
2. Запускает прогон → видит прогресс → видит, что все 11 текстов опубликованы
   (либо ровно столько, сколько хватило админок).
3. Скачивает CSV — там корректные URL.
4. Повторно запускает прогон на тех же 11 текстах (загрузив новый архив) —
   те же админки повторно не используются. Если админок не хватило —
   статус `need_more_admins`.
5. После кнопки «сбросить историю» админки снова доступны для проекта.
6. Все воркеры стабильно работают; завершение / пауза / отмена работают
   корректно. Если контейнер с Celery упал — после рестарта прогон
   автоматически помечается как `interrupted`, незавершённые text_items
   возвращаются в `pending`, владелец видит кнопку Resume.
7. **Перформанс-чек** на horror-стенде: загенерить 1М фейковых text_items
   в 1000 прогонов; страница `/projects` рендерится <200мс, страница
   прогона <100мс, `pick_wp_access` <10мс при 50К админок в пуле.
   `EXPLAIN ANALYZE` всех hot-path запросов прикладывается к PR
   `feat/db-models-...`.

---

## 11. Открытые вопросы

1. Шифрование паролей админок в БД — Fernet с ключом из env? Включаем
   с самого начала или после первого прогона?
2. Что считать «количество подходящих админок для проекта» — все валидные
   минус использованные? Учитывать ли error_counter порог (как в Flask,
   `< 5`)?
3. Минимальный лимит на размер архива/число текстов в одном прогоне?
4. Нужна ли загрузка папкой через drag-and-drop как альтернатива zip
   (через `webkitdirectory`)? В Flask только zip.
5. По умолчанию `concurrency=25` — окей? В Flask было 15. На стейдже
   подберём.
