# Этап 3. Дешборды, real-time, валидатор, прокси, нотификации

## Цель этапа

Закрыть наблюдаемость, отказоустойчивость и UX-обвязку вокруг ядра постинга.
ТЗ называет это «Этап 2 (Ресурсы)» — глобальные дешборды и реальное
отслеживание прогресса. Мы сюда же добавляем валидатор админок, прокси,
нотификации, шифрование паролей и предпросмотр HTML.

## Что входит

1. SSE для прогресса прогона (заменяем polling)
2. Глобальные дешборды:
   - Очередь всех текстов (по всем пользователям) для super_admin
   - Все валидные админки и сколько ещё доступно для каждого проекта
   - Активные прогоны и их статусы
3. Валидатор админок WP (XML-RPC ping + опц. определение языка):
   - По расписанию (TaskIQ scheduled cron)
   - По кнопке в UI
4. Прокси-серверы:
   - Хранение списка прокси
   - Привязка прокси к админке/проекту/прогону
   - Использование в воркере httpx
5. Шифрование паролей в `wp_accesses` (Fernet)
6. HTML preview контента текста (визуальный редактор a-la WP)
7. Нотификации:
   - In-app тосты для текущего юзера через SSE
   - Email-нотификация о завершении прогона (через SMTP, опц. Mailu в стеке)
   - Заготовка под телеграм-бот (на будущее)
8. Прометей-метрики и Grafana (опционально)
9. Audit log на основные действия

## Что НЕ входит

- Developer API (этап 4)
- Indexing, Spintax, AI (этап 5)

---

## 1. SSE для прогресса

### 1.1 Бэкенд

`GET /admin/api/postings/{run_id}/events` — `text/event-stream`.

При апдейте счётчиков воркер публикует в Redis pub/sub канал
`run:{run_id}` JSON-событие:
```json
{ "event": "progress",
  "data": {"posted": 42, "failed": 3, "total": 100, "current_text_id": 17} }
```

FastAPI endpoint:
```python
async def stream_events(run_id, user):
    require_access_to_run(user, run_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"run:{run_id}")
    async def gen():
        # сначала отдаём текущий снимок
        yield format_event("snapshot", await get_run_snapshot(run_id))
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                yield format_event("progress", msg["data"])
    return StreamingResponse(gen(), media_type="text/event-stream")
```

### 1.2 Фронт

`new EventSource('/admin/api/postings/{id}/events')` — обновляем
прогресс-бар без polling. Автореконнект встроен.

### 1.3 Альтернативный канал «глобальный»

`GET /admin/api/events/global` — стрим событий обо всех прогонах текущего
юзера. Используется на дешборде «мои очереди».

---

## 2. Глобальные дешборды

### 2.1 Endpoints

- `GET /admin/api/dashboard/global-queue` — общая сводка для super_admin:
  - сколько активных прогонов
  - сколько текстов в очереди (status='pending' || 'posting') суммарно
  - сколько админок свободно (валидных, не привязанных к текущим
    прогонам — глобально или per project)
- `GET /admin/api/dashboard/my-queue` — то же для текущего юзера.
- `GET /admin/api/dashboard/wp-pool-summary` — суммарно по пулу админок:
  - всего, валидных, инвалидных, по тегам.
- `GET /admin/api/dashboard/project-pool-summary?project_id=X` — для
  конкретного проекта: сколько админок ещё не использовалось.

### 2.2 Оптимизация запросов

Все агрегации опираются на денормализованные счётчики из этапа 2 +
агрегатные view:

```sql
CREATE MATERIALIZED VIEW mv_wp_pool_summary AS
SELECT
  COUNT(*) FILTER (WHERE is_valid) AS valid_count,
  COUNT(*) FILTER (WHERE NOT is_valid) AS invalid_count,
  COUNT(*) AS total
FROM wp_accesses;

-- REFRESH каждые 30 сек TaskIQ scheduled (CONCURRENTLY если построим
-- уникальный индекс)
```

### 2.3 UI

`/dashboard` для super_admin — четыре карточки сверху (queues, runs,
admins valid, admins free) + два графика (за сегодня: posts done, errors).

`/dashboard` для manager — те же карточки, но scope=мои.

---

## 3. Валидатор админок

### 3.1 TaskIQ scheduled

`tasks.validate_wp_accesses` — cron `0 */4 * * *` (каждые 4 часа).

Логика:
```python
async def validate_wp_accesses(batch_size=200):
    while True:
        batch = await fetch_oldest_validated(batch_size)
        if not batch:
            return
        sem = asyncio.Semaphore(20)
        async def check(acc):
            async with sem:
                ok = await ping_xmlrpc(acc)
                await save_validation_result(acc, ok)
        await asyncio.gather(*(check(a) for a in batch))
```

`ping_xmlrpc(acc)`:
- POST `wp.getProfile` или `system.listMethods` с auth → получаем 200.
- Иначе increment error_counter, при error_counter > N (например, 5)
  ставим `is_valid=false`.
- При успехе: error_counter=0, last_validated_at=now, опц. определяем
  `language` через анализ контента главной страницы (выкинули из Flask).

### 3.2 Endpoints

- `POST /admin/api/wp-accesses/validate` — запустить on-demand
  (для super_admin). Возвращает task_id; результаты через SSE на
  странице.
- `GET /admin/api/wp-accesses/validation-status` — текущий статус
  активной валидации (если запущена).

### 3.3 UI

На странице `/wp-accesses` — кнопка «Проверить все», прогресс в real-time
(SSE). По окончании показывает summary: валидных X, инвалидных Y, удалено
(или помечено) Z.

---

## 4. Прокси

### 4.1 Модель

```
proxies
  id, name, scheme (http|https|socks5), host, port,
  username (nullable), password (nullable, encrypted),
  is_active, last_checked_at, last_error,
  tag (TEXT), created_at, owner_user_id (nullable — глобальный, если NULL)
```

Связи (на выбор архитектуры — один прокси на прогон достаточно для MVP):
- `posting_runs.proxy_id` (FK proxies, nullable) — этот прогон ходит
  через этот прокси.
- Опционально на будущее: pool прокси с round-robin.

### 4.2 Использование в воркере

```python
async with httpx.AsyncClient(proxy=run.proxy_url, timeout=...) as client:
    ...
```

### 4.3 UI

`/proxies` (super_admin) — CRUD. На форме создания прогона —
dropdown выбора прокси.

### 4.4 Health-check прокси

TaskIQ scheduled — раз в час: пингует прокси через `httpbin.org/ip` или
свой эндпоинт. При фейле — `is_active=false`.

---

## 5. Шифрование паролей админок

Fernet (cryptography lib), ключ из env `WP_ACCESS_ENC_KEY`.

- При записи: `password = fernet.encrypt(plain).decode()`.
- При чтении в воркере: `fernet.decrypt(stored).decode()`.
- Миграция данных: data-migration, которая возьмёт все существующие
  plaintext-пароли и зашифрует на месте.
- На UI пароли не показываются никогда; только маска `••••••••`.

---

## 6. HTML preview

На странице загруженного прогона — для каждого text_item кнопка «👁
preview», которая открывает модалку с отрендеренным HTML в `iframe`
с sandbox. Это «визуальный редактор WP» из ТЗ.

Опционально: предпросмотр в стиле дефолтной WP-темы (TwentyTwentyFour) —
просто подключаем её CSS как preset.

---

## 7. Нотификации

### 7.1 In-app

Через SSE-канал `events/me` — события `notification.created`. UI
показывает тосты + бэдж на колокольчике в topbar.

Хранение: таблица `notifications`:
```
id, user_id (FK), kind (run_finished|run_failed|validator_done|...),
title, body, link (optional), read_at (nullable), created_at
```

### 7.2 Email

Только если у пользователя задан `email`.
- В `.env` указывается SMTP (host/port/user/pass/from).
- TaskIQ task `send_email(to, subject, body_html, body_text)`.
- Темплейты — Jinja2 в `backend/src/templates/email/`.

Заготовка под Mailu: положить `docker-compose.mailu.yml` в `deploy/`
(референс из server_app), но НЕ включать по умолчанию. Решение
поднимать Mailu — отдельно.

### 7.3 События для нотификаций

- `run_finished` — прогон завершён успешно.
- `run_failed` — прогон упал.
- `run_need_more_admins` — закончились админки.
- `validator_finished` — массовая валидация завершена (только админу,
  который её запустил).

---

## 8. Метрики и observability

### 8.1 Прометей-метрики FastAPI

`prometheus-fastapi-instrumentator` → `/metrics`.

Кастомные метрики:
- `posting_run_active` (gauge)
- `posting_xmlrpc_requests_total` (counter, label outcome)
- `posting_xmlrpc_duration_seconds` (histogram)
- `wp_accesses_valid_count` (gauge)

### 8.2 Логи

Структурированный JSON-логгер (`structlog`). Trace-id через middleware,
прокидывается в Celery/TaskIQ context для корреляции.

### 8.3 Grafana (опционально)

Запускаем `prometheus + grafana + loki` отдельным docker-compose
файлом `docker-compose.monitoring.yaml`. Дашборды коммитим как JSON.

---

## 9. Audit log

Таблица:
```
audit_log
  id, user_id (FK), action (TEXT), resource_type, resource_id,
  changes (JSONB), ip, user_agent, created_at
```

Покрываем: создание/удаление user, group, role, project, posting_run,
wp_access; reset_admin_usage; запуск прогона; pause/cancel.

Endpoint `GET /admin/api/audit-log` (super_admin) с фильтрами.

---

## 10. UI новые/обновлённые страницы

- `/dashboard` — карточки + графики (заменить заглушку этапа 1).
- `/wp-accesses` — добавить кнопку «проверить все», прогресс
  валидатора через SSE.
- `/proxies` — новая.
- `/notifications` — лента нотификаций + настройки.
- `/audit-log` — новая (super_admin).

---

## 11. Декомпозиция на feat-ветки

1. `feat/sse-run-progress`
2. `feat/global-dashboards-api`
3. `feat/ui-dashboards`
4. `feat/wp-accesses-validator-scheduled`
5. `feat/wp-accesses-validator-on-demand-and-ui`
6. `feat/proxies-crud`
7. `feat/proxies-in-worker`
8. `feat/encrypt-wp-passwords`
9. `feat/html-preview-modal`
10. `feat/notifications-model-and-sse`
11. `feat/email-notifications`
12. `feat/audit-log`
13. `feat/prometheus-metrics`
14. `chore/grafana-monitoring-compose` (optional)

---

## 12. Критерии приёмки

1. Прогресс прогона на странице обновляется в реальном времени без F5.
2. super_admin видит на дашборде корректные числа активных прогонов,
   очереди, валидных админок; цифры совпадают с прямым SQL `COUNT`.
3. Валидатор админок по расписанию запускается и корректно помечает
   мёртвые. Прокатываем эксперимент: добавить 5 заведомо рабочих и 5
   заведомо неживых — после валидатора в БД именно 5/5.
4. Прогон через прокси работает (проверяем через IP-отлов на
   собственном тестовом сервере или httpbin).
5. Email-нотификация о завершении прогона приходит на указанный адрес.
6. Все пароли админок в БД зашифрованы; на UI не видны.
7. Audit log заполняется на ключевые действия.

---

## 13. Открытые вопросы

1. SMTP-провайдер для рассылки — внутренний Mailu или внешний
   (SendGrid/SES/Postmark)? Если Mailu — нужен публичный домен и MX.
2. Где брать прокси? Закупаем у провайдера? Какой формат
   импорта (CSV `host;port;user;pass;type`)?
3. Какой порог `error_counter` для invalidate админки? В Flask было 5.
4. Стоит ли поднимать Grafana сразу или достаточно `/metrics` +
   alertmanager без визуализации?
