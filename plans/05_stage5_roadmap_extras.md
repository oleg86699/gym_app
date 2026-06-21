# Этап 5. Roadmap-доработки: Indexing, Spintax, AI, главные страницы WP

## Цель этапа

Доделать перечисленные в ТЗ-2026 пункты «Этап 3 (Доработки)»:
- Indexing: автоматическая отправка созданных ссылок в Google Indexer.
- Content: интеграция Spintax (синонимайзера) и генерации текста через ИИ.
- Links: постинг ссылок с главных страниц WordPress-сайтов (отдельная
  логика, под отдельное ТЗ).

Также сюда включаем мелкие доработки UX/UI, которые накопятся после
этапов 1–4.

Этот этап разбивается на независимые **подэтапы**, которые можно делать в
любом порядке.

---

## 5A. Google Indexing API

### Цель

Каждый успешный пост автоматически отправляется в Google Indexing API для
ускоренной индексации.

### Что делать

1. **Регистрация Service Account** в Google Cloud:
   - Создать сервис-аккаунт, выдать роль Owner для каждого WP-сайта в
     Search Console.
   - JSON-ключ хранится в `secrets/google-indexing.json`, путь — в env.
2. **Модель**:
   ```
   indexing_requests
     id, text_item_id (FK), url, type (URL_UPDATED|URL_DELETED),
     status (pending|sent|failed|skipped),
     google_response (JSONB), attempts, last_error, sent_at, created_at
   ```
3. **TaskIQ task** `submit_to_google_indexing(text_item_id)` — вызывает
   `https://indexing.googleapis.com/v3/urlNotifications:publish` с
   `type=URL_UPDATED`. На фейл — retry 3 раза с задержкой.
4. **Trigger**: после `record_success` в воркере постинга — enqueue task.
5. **Лимиты**: Google даёт 200 запросов/день, надо квотировать. Если
   квота на сегодня исчерпана — откладываем на завтра (`status=pending`,
   scheduled cron подбирает).
6. **UI**: в детальном виде text_item — статус индексации; на дешборде
   проекта — счётчик `indexed_count`.
7. **Опция**: разрешить пользователю отключить indexing для проекта
   (поле `auto_indexing` в `projects`).

### Подзадачи

- `feat/google-indexing-models-and-task`
- `feat/indexing-trigger-and-quota`
- `feat/ui-indexing-status`

---

## 5B. Spintax + AI генерация контента

### Цель

Уменьшить ручную работу по подготовке текстов:
- **Spintax**: один шаблон вида `{Hello|Hi|Hey}, world!` разворачивается в
  N уникальных вариантов.
- **AI генерация**: по запросу/шаблону создаём текст через OpenAI/Claude
  API.

### Что делать

#### Spintax

1. **Модуль `core/spintax.py`** — парсит `{a|b|c}` рекурсивно, выдаёт
   один случайный вариант (или все варианты при заданном seed).
2. **Endpoint** `POST /admin/api/spintax/expand`:
   - body: `{ template: str, count: int }`
   - response: `[ str, ... ]`
3. **Применение в загрузке текстов**: если файл содержит spintax-теги —
   разворачиваем при создании text_items (один файл → N text_items с
   разными вариантами). Опционально.

#### AI

1. Подключение OpenAI/Anthropic SDK (env-based credentials).
2. **Модель**:
   ```
   ai_generation_requests
     id, user_id, prompt_template, params (JSONB), model,
     status, generated_count, cost_usd, created_at
   ai_generation_outputs
     id, request_id (FK), title, content, storage_key,
     used_in_text_item_id (nullable)
   ```
3. **Endpoint** `POST /admin/api/ai/generate-batch`:
   - body: `{ topic, count, length_range, language, model }`
   - запускает TaskIQ task, который генерирует N статей и складывает в
     ai_generation_outputs.
4. **UI**: страница `/ai-content` — форма генерации, список последних,
   возможность «отправить пачку в проект» (создание postings из готового).
5. **Cost tracking**: каждый запрос пишет стоимость; админ видит общий
   расход по месяцу.

### Подзадачи

- `feat/spintax-engine`
- `feat/spintax-in-upload`
- `feat/ai-generation-models-and-tasks`
- `feat/ui-ai-content-page`
- `feat/ai-cost-tracking`

---

## 5C. Постинг ссылок с главных страниц WP

### Цель

Отдельный режим: вместо публикации нового поста — редактируется главная
страница сайта так, чтобы там появился блок со ссылкой на заданный URL.

### Замечание

ТЗ говорит: «совсем другая логика работы, будет отдельное ТЗ». Поэтому в
плане только заглушка:
- Архитектурно: новая таблица `link_placements`, новый Celery task
  `place_link_on_homepage`, новый UI-раздел.
- Конкретные шаги — после получения детального ТЗ.

---

## 5D. UX/UI доработки (накопленные)

После этапов 1–4 ожидаемо появится список мелких улучшений. Ведём его в
GitHub Issues с label `enhancement`. Тут перечислим то, что уже сейчас
видно как нужное:

- **Drag-and-drop загрузка** .zip и .csv (вместо клика на input).
- **Поиск/фильтр** во всех таблицах админки.
- **Bulk-actions** на странице WP-админок: удалить отмеченные, изменить
  тег у отмеченных.
- **Импорт админок из текста**: paste в textarea (по строке `domain;login;password`).
- **Bookmark-able фильтры**: query-params в URL для таблиц.
- **Dark mode toggle** (если в этапе 1 пропустили).
- **Локализация** EN/RU (если оставляли только один язык изначально).
- **Импорт/экспорт проектов** между инстансами (тот самый «битые сейвы» из ТЗ).

---

## 5E. Тех-долг и оптимизация

К этому этапу накопится:
- Замена raw-SQL мест на ORM, если останутся.
- Materialized views для тяжёлых дешбордов, если просели по
  perf-метрикам.
- Sharding/partitioning `text_items` по `posting_run_id`, если таблица
  выросла до десятков миллионов.
- Замена Celery на TaskIQ-только, если на практике Celery оказался
  избыточен.
- Профилировка холодного старта app-контейнера и оптимизация Dockerfile
  (multi-stage, кеш слоёв).
- Подключение Sentry для трекинга ошибок.

---

## Критерии приёмки

Этап 5 закрывается **по подэтапам** независимо. Полное закрытие — после
того, как все 5A/5B/5C/5D отмечены как done с зелёными e2e.

---

## Открытые вопросы

- Стоит ли делать AI генерацию **on-the-fly во время постинга**
  (каждый пост уникален), или только **пред-генерация** в библиотеку
  ai_generation_outputs, откуда тексты потом загружаются обычным
  upload-ом? Второй вариант проще и предсказуемее по cost.
- Какой провайдер для AI? Anthropic Claude vs OpenAI vs Gemini.
  Влияет на промпт-инжиниринг и cost-trackingа.
- Indexing API — нужен ли для всех WP-сайтов или только для тех, где у
  нас есть Search Console доступ?
