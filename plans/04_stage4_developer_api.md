# Этап 4. Developer API + документация

## Цель этапа

Дать программируемый доступ к функциональности проекта внешним разработчикам/
скриптам. Это второй неймспейс эндпоинтов (`/api/v1/`), независимый от
`/admin/api/`. Заложить фундамент так, чтобы дальше можно было добавлять
новые ресурсы без изменения авторизационной обвязки.

## Что входит

1. API keys (генерация, scope, expiration)
2. Авторизация по `X-API-Key` header
3. Rate limiting per key
4. Версионированный неймспейс `/api/v1/`
5. Базовые ресурсы (минимум, чтобы можно было программно запускать прогоны):
   - `/api/v1/projects` (read, create)
   - `/api/v1/postings` (create with text_items inline или multipart,
     list, get, start, cancel)
   - `/api/v1/wp-accesses` (read, import)
   - `/api/v1/health`
6. Идемпотентность мутаций (`Idempotency-Key` header)
7. OpenAPI спецификация + Swagger UI + ReDoc на отдельном пути
8. Webhook'и (опционально): уведомление внешнего URL о завершении прогона
9. UI: страница «API Keys» в профиле и админ-панели
10. Документация для разработчиков:
    - Markdown в репозитории + статический сайт на VitePress или
      просто `/docs/v1/` отдаваемый из FastAPI

---

## 1. Модели

```
api_keys
  id, name, owner_user_id (FK),
  hashed_key (CHAR(64)),       -- sha256 от ключа, оригинал не храним
  prefix (CHAR(8)),            -- для отображения "pzpk_abcd1234..."
  scopes (TEXT[]),             -- ['projects.read', 'postings.write', ...]
  rate_limit_per_minute (INT default 60),
  expires_at (TIMESTAMPTZ, nullable),
  last_used_at, last_used_ip,
  is_active, created_at, revoked_at

api_request_log
  id, api_key_id (FK, nullable), path, method, status_code,
  duration_ms, ip, user_agent, created_at

webhook_endpoints
  id, owner_user_id, url, secret (для HMAC), events (TEXT[]),
  is_active, created_at, last_called_at, last_status

webhook_deliveries
  id, endpoint_id, event_name, payload (JSONB),
  attempts, last_attempt_at, last_status_code, delivered_at
```

---

## 2. Авторизация

`Depends(get_current_api_key)`:
- Берёт `X-API-Key` header.
- Считает sha256, ищет в `api_keys.hashed_key`.
- Проверяет `is_active`, `expires_at`.
- Проверяет scope для эндпоинта.
- Логирует в `api_request_log`.
- Возвращает `ApiKey` + связанного `User`.

Пользователь под API-ключом наследует scope владельца ключа + ограничения
самого ключа. Т.е. если у юзера нет роли super_admin, его API-ключ тоже не
может смотреть чужие проекты, даже если scope-ом `projects.read.all`.

---

## 3. Rate limiting

`slowapi` или собственный middleware с Redis:
- Ключ ratelimit: `rl:{api_key_id}:{minute_window}`.
- INCR в Redis с TTL 60 сек. Если > `rate_limit_per_minute` → 429 +
  `Retry-After`.

Дополнительно: глобальный лимит 100 req/sec на IP для защиты от DoS.

---

## 4. Идемпотентность

Header `Idempotency-Key: <uuid>` на мутирующих запросах.
- Кешируем `(api_key_id, idempotency_key) → response` в Redis на 24 ч.
- Повторный запрос с тем же ключом — возвращает первый ответ, не
  выполняет мутацию повторно.

---

## 5. Эндпоинты v1

Контракт минималистичный, RESTful.

### Projects

- `GET /api/v1/projects` — список (своих).
- `POST /api/v1/projects` — `{ name }`.
- `GET /api/v1/projects/{id}` — детали.

### Postings (runs)

- `POST /api/v1/postings`:
  - body: `{ project_id, name, period_days, concurrency, scheduled_for,
    texts: [{filename, content_base64}], proxy_id? }`
  - или multipart с архивом
  - возвращает `{ id, status: "draft" }`
- `POST /api/v1/postings/{id}/start` — поставить в очередь.
- `GET /api/v1/postings/{id}` — статус + счётчики.
- `GET /api/v1/postings/{id}/text-items` — пагинированный список.
- `GET /api/v1/postings/{id}/result.csv`
- `POST /api/v1/postings/{id}/cancel`

### WP Accesses

- `GET /api/v1/wp-accesses` — пагинация + фильтры.
- `POST /api/v1/wp-accesses/import` — `{ items: [{domain, login, password}, ...] }`
  или CSV multipart.

### Health/system

- `GET /api/v1/health` — без авторизации.
- `GET /api/v1/whoami` — кто я (по ключу).

---

## 6. Версионирование

- Все эндпоинты живут под `/api/v1/`.
- В response header `X-API-Version: 1`.
- При выпуске v2 (когда понадобится) — параллельно, v1 deprecated за 6
  месяцев до удаления.
- Брейкинг изменения внутри v1 — запрещены.

---

## 7. OpenAPI и документация

- FastAPI генерит OpenAPI 3.1 автоматически.
- Swagger UI: `/api/v1/docs`.
- ReDoc: `/api/v1/redoc`.
- Каждый эндпоинт имеет `summary`, `description`, примеры request/response.
- Сгенерированный JSON-spec публикуется в репо (`docs/openapi.json`) на
  CI каждый релиз — для версионирования контракта.

### Документация для разработчиков

`docs/api/` — VitePress сайт (или Markdown в репо для начала):
- Quickstart (получение ключа, hello world запрос)
- Авторизация
- Rate limits и идемпотентность
- Reference (генерится из OpenAPI через `widdershins` или вручную)
- Webhooks
- Changelog API
- SDK на Python (тонкий клиент через `httpx` — генерится из OpenAPI
  через `openapi-python-client`)

Деплой документации — GitHub Pages из `docs/`.

---

## 8. Webhooks

### 8.1 Регистрация

`POST /api/v1/webhooks` — `{ url, events, secret? }`.

### 8.2 Доставка

При наступлении события (`posting.finished`, `posting.failed`,
`wp_access.invalidated`):
- В TaskIQ enqueue `deliver_webhook(endpoint_id, payload)`.
- Подписываем HMAC-SHA256 от тела `secret`-ом, header `X-Signature`.
- Retry: 3 попытки с экспоненциальной задержкой (1m, 5m, 30m).
- При финальном фейле — `last_status` сохраняется, отправляем
  in-app/email уведомление владельцу.

### 8.3 UI

`/api-settings/webhooks` — CRUD + список последних доставок.

---

## 9. UI

### 9.1 API Keys

Страница `/api-keys` (страница профиля + страница для super_admin):
- Список своих ключей (id, name, prefix, scopes, expires, last_used).
- Кнопка «Создать»: модалка с именем, scopes (чекбоксы), expiration.
- При создании показывается **полный ключ один раз** + кнопка copy.
  Дальше доступен только prefix.
- Кнопка «Revoke».

### 9.2 Webhooks

`/api-settings/webhooks` (опционально в этом этапе).

### 9.3 Public API docs page

`/developer` — лендинг с ссылкой на `/docs/api/` (внешний или
встроенный), кнопкой «получить ключ».

---

## 10. Декомпозиция на feat-ветки

1. `feat/api-keys-models-and-endpoints`
2. `feat/api-keys-auth-dependency`
3. `feat/rate-limiting`
4. `feat/idempotency-middleware`
5. `feat/v1-projects-endpoints`
6. `feat/v1-postings-endpoints`
7. `feat/v1-wp-accesses-endpoints`
8. `feat/openapi-tuning-and-examples`
9. `feat/api-docs-site`
10. `feat/ui-api-keys-page`
11. `feat/webhooks` (опционально)

---

## 11. Критерии приёмки

1. Юзер создаёт API key через UI, копирует, использует в curl.
2. `curl -H "X-API-Key: $K" /api/v1/projects` отдаёт его проекты.
3. Программно через API:
   - создан проект,
   - загружены тексты + WP-админки,
   - запущен прогон,
   - получен CSV результата.
4. Превышение rate limit возвращает 429.
5. Повтор запроса с тем же `Idempotency-Key` не создаёт дубль.
6. Swagger UI открывается, все эндпоинты задокументированы с примерами.
7. (если делаем webhooks) — webhook доставляется на свой сервер, подпись
   валидна.

---

## 12. Открытые вопросы

1. Нужны ли OAuth2 client credentials в дополнение к статическим
   API-ключам? Имеет смысл, если будут сторонние приложения с UI.
2. Делаем ли SDK сразу для Python? Для JS/TS?
3. Доменное имя для API: `api.<host>` или просто `/api/v1` под основным
   хостом? Влияет на CORS.
4. Webhook'и в MVP этапа 4 или отложить отдельно?
