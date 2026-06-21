# Редизайн пайплайна генерации + постинга

> Живой документ. Статусы пунктов: ☐ не начато · 🟡 в работе · ☑ готово.
> Обновляем по ходу реализации. Последнее обновление: создание плана.

## 0. Контекст и проблема

**Как сейчас** (`domain/content_engine/campaign.py::generate_campaign_run`):
- И **auto**, и **manual** генерируют **весь файл сразу**, и только потом постят:
  `generate_campaign_run` прогоняет цикл по всем строкам × count до конца → `_finalize_run` ставит `auto → QUEUED + enqueue постинга`, `manual → READY`.
- Для `gen_per_post` AI-вызовы **последовательные** (`await _gen` в `for`). 5000 текстов = 5000 запросов подряд до первого поста.
- **Drip (`spread_days`) не применяется к генерации** — весь файл генерится в день 1, по дням размазан только постинг.

**Чем плохо:** долгое ожидание на больших/многодневных файлах, занятая очередь/ресурсы, зря потраченный AI-бюджет при паузе/отмене.

**Режимы генерации (подтверждено по коду):**
- `gen_per_post` — уникальный AI-текст на КАЖДЫЙ айтем (`count` AI-вызовов на строку).
- `gen_per_row` — 1 AI-текст («оригинал», `item[0]`) на строку, остальные (`item[1..]`) — **спины** оригинала (без AI, через spin-формулу, заполняются на Start).
- `reuse` — без AI, reusable-оригиналы из библиотеки + спин.

## 1. Целевая модель

### Auto-режим
- Start **всегда ручной**.
- После Start генерация и постинг идут **параллельно**: айтемы, у которых появился текст, сразу берутся в постинг. Не ждём весь файл.
- При drip (N дней) — генерим тексты **этого дня** и постим их параллельно; не грузим систему всеми тысячами текстов из файла, только нужными в этот день.
- Пауза/отмена **останавливают и генерацию** → не жжём AI-бюджет.

### Manual-режим
- На создании задачи **тексты НЕ генерим**. Создаём айтемы-плейсхолдеры (без текста).
- Кнопка **«Сгенерировать тексты»** — фоном генерит все тексты задачи (с прогрессом, отменяемо).
- Кнопка **«Старт постинга»** — постит айтемы, у которых уже есть текст.
- Полный контроль через пер-айтем кнопки (см. ниже).

### Пер-айтем кнопки (последний столбец таблицы, иконки-only + тултипы)
Состав зависит от `content_mode` и (для `gen_per_row`) типа айтема (оригинал/спин).

| Состояние айтема | `gen_per_post` / `upload_txt` / `csv_direct` | `gen_per_row` оригинал | `gen_per_row` спин |
|---|---|---|---|
| нет текста (`text_id IS NULL`) | ⚡ Сгенерировать (AI) | ⚡ Сгенерировать (AI) | ⚡ Сгенерировать (спин из оригинала) |
| есть текст, `pending`/`failed` | ↻ Перегенерировать (AI) · ▶ Запостить | ↻ Перегенерировать (AI) · ▶ Запостить | ↻ Переспиннить · ▶ Запостить |
| `posting` (в работе) | спиннер | спиннер | спиннер |
| `posted` | ↻ Repost | ↻ Repost | ↻ Repost |

- **Generate / Regenerate** — только для gen-режимов (`csv_campaign`). Для `upload_txt` / `csv_direct` текст уже есть и AI не нужен.
- **Post / Repost** — общий механизм **для всех** типов задач, включая `sitewide_link` / `homepage_link` (там Repost = переразместить ссылку перебором сайтов).

## 2. Сквозные решения (фиксируем до кода — дорого ретрофитить)

### 2.1 Гейтинг постинга по готовности текста
`_pick_pending_batch` (`workers/celery/posting.py`) добавляет условие:
```
TextItem.status == PENDING
AND (not_before IS NULL OR not_before <= now)
AND TextItem.text_id IS NOT NULL        # ← НОВОЕ: постим только готовые
```
Так постинг-воркер сам подхватывает «созревшие» айтемы, а генератор их кормит.
> Для `upload_txt`/`csv_direct`/link айтемов `text_id`/контент уже есть — условие не мешает (для link контент в `link_url`, а не `text_id`; гейт применяем только к `csv_campaign`-ранам, либо делаем предикат «есть чем постить» с учётом типа).

### 2.2 Статусная модель «генерит И постит одновременно»
- Ран остаётся в `RUNNING` весь пайплайн.
- Прогресс генерации — в `gen_params.gen_done/gen_total` (уже есть; красный бар в Global Queue добавлен).
- Прогресс постинга — в счётчиках `posted/failed/skipped`.
- UI рана/очереди показывает оба: «генерация X/Y» + «постинг K/N».
- Ран **не финишируется**, пока: генерация не завершена ИЛИ остались не-терминальные айтемы. (см. 2.6)

### 2.3 Идемпотентность / claim пер-айтем экшенов
- Каждый пер-айтем экшен **клеймит айтем** (status → промежуточный, напр. `generating`/`posting`) атомарно, чтобы:
  - bulk-генерация и ручная «сгенерировать» на одном айтеме не задвоились;
  - параллельные клики не запустили двойной постинг.
- Нужен либо новый под-статус `generating` для айтема, либо флаг claim в `gen_params`/отдельном поле. **Решение:** добавить `TextItemStatus.GENERATING` (по аналогии с `POSTING`).

### 2.4 Семантика Repost (важно)
Запощенный айтем уже занял сайт (`project_wp_used`) и имеет `posted_url`. Repost:
- сбрасывает айтем в `pending` (текст сохраняем);
- перебирает сайты заново (hopping), **исключая текущий сайт** (он «не показал пост»);
- постит, обновляет `site_id`/`credential_id`/`posted_url`/`posted_at`;
- **съедает ещё один слот сайта** (`project_wp_used` += запись) → влияет на `max_posts_per_site` задачи.
- Англ. термин в UI: **Repost** (иконка ↻).

### 2.5 Классификация типа айтема (для gen_per_row)
- оригиналы: `fanout_groups[].original_item_id` / `main_text_ids`;
- спины: `fanout_groups[].spin_item_ids`.
- Хелпер `classify_item(run, item) -> {mode, is_original, group}` → драйвит поведение Generate/Regenerate.

### 2.6 Завершение пайплайна
Посыл-воркер при «нет готовых due-айтемов»:
- если генерация ещё идёт ИЛИ есть айтемы без текста с близким `not_before` → **re-arm** (как drip: `scheduled` или короткий повторный заход), не финишируем;
- если генерация завершена И все айтемы терминальны (`posted`/`failed`/`skipped`) → `done`;
- если генерация завершена, но не на что постить (нет сайтов) → `need_more_admins` (как сейчас).

### 2.7 Drip-aware генерация
- Генератор обрабатывает только айтемы с `not_before <= now + горизонт` (напр. сутки), затем re-arm до ближайшего `not_before`.
- Тот же cron-механизм, что у drip-постинга (`dispatch_scheduled_runs`).

## 3. Фазы

### Фаза 1 — пер-айтем экшены + кнопки + manual «не генерим сразу» + Repost везде
Самостоятельная ценность, на текущей архитектуре (без streaming).

**Бэкенд:**
- ☑ `TextItemStatus.GENERATING` (enum; `status` — plain String, миграция не нужна).
- ☑ Standalone-генерация одного айтема `generate_item(item_id, *, regenerate=False)` (`campaign.py`):
  - ☑ `gen_per_post`: `_gen` → новый Text → `text_id` (claim GENERATING → PENDING).
  - ☑ `gen_per_row` оригинал: `_gen` → `Text.body` оригинала + сброс `spin_formula`.
  - ☑ `gen_per_row` спин: `_spin_one` → `make_variant` (переспин, без AI) → новый variant Text.
  - ☑ `text_items.gen_row JSONB` (миграция 0041) — заполняется в `_materialize_one`/`_create_group_items`.
  - ☑ TaskIQ `content.generate_item` (async, UI поллит GENERATING→PENDING).
- ☑ Standalone-постинг одного айтема `_post_one_item_standalone(item_id, *, is_repost)` (`workers/celery/posting.py`) — переиспользует `_post_one_item` (post) / `process_link_item` (link), собирая shared-объекты на один айтем. Celery task `postings.post_one_item`.
- ☑ `repost` — `is_repost=True`: сброс posted→pending + `registry.mark_exhausted(old_site)` (для link `used_sites={old_site}`) → новый сайт, +слот.
- Эндпоинты (mirror `text-items/{item_id}/remove-link`):
  - ☑ `POST /postings/{run_id}/text-items/{item_id}/generate`  (202; guard: нет текста + gen-задача)
  - ☑ `POST .../regenerate` (202; guard: есть текст + gen-задача)
  - ☑ `POST .../post`  (202, enqueue Celery; guards: not posted, есть текст для post-типа)
  - ☑ `POST .../repost` (202; guard: только posted)
  - Permissions: `manage` на ране (как pause/resume). ☑
- ☑ Manual create: НЕ enqueue генерацию; **пред-создаём ПУСТЫЕ айтемы** (видны в таблице сразу, `text_id=NULL`), ран `READY`. `create_empty_campaign_items`:
  - `gen_per_post`: `sum(count)` пустых айтемов (`gen_row` сохранён).
  - `gen_per_row`: плейсхолдер-оригинал (пустое тело) + группа (оригинал+спины, все `text_id=NULL`) + `fanout_groups`/`deferred_fanout`.
  - gen_params: `gen_done=0, gen_total=` (оригиналы для gen_per_row / все для per_post) → драйвит кнопку. Проверено живьём: gen_per_row 2 строки → 2 пустых айтема, `gen 0/1`, READY.
- ☑ Кнопка «Сгенерировать тексты» (bulk) — `POST /postings/{run_id}/generate-texts` (гард: есть пустые targets, иначе 409) → `generate_run_items` (gen_per_row → оригиналы, спины на Start; gen_per_post → все пустые). Статус→`unpacking` (ген-бар в очереди). Пер-айтем generate работает и на пустых строках ДО bulk.
- ☑ **Text-гейт постинга** (§2.1, подтянут сюда): `_pick_pending_batch(require_text=True)` для post-типа — постим только айтемы с готовым `text_id`/`storage_key`, пустые gen-айтемы пропускаем. Link-путь без гейта.
- ☑ UI: кнопки «Сгенерировать тексты» (rose, Wand2) и «Старт постинга» в шапке manual gen-рана.
- ☑ Repost обобщён на post/csv/link (link через `process_link_item` с `used_sites`).

**Фронтенд:**
- ☑ Колонка «Действия» в таблице айтемов (`runs/[id]/+page.svelte`): иконки Wand2/RefreshCw/Send/RotateCw + тултипы, состояния по статусу (posted→Repost; pending+нет текста→Generate; pending+текст→Regenerate+Post; posting/generating→спиннер).
- ☑ Статус `generating` как first-class: счётчик в `run_progress_counts` + `RunProgressResponse` + TS `RunProgress` + вкладка-фильтр + цвет бейджа (rose).
- ☑ api-клиент (`generateItem`/`regenerateItem`/`postItem`/`repostItem`) + типы (`TextItemStatus += generating`).
- ☐ Кнопки «Сгенерировать тексты» / «Старт постинга» в шапке manual-рана (часть manual-no-upfront).

**Тесты:**
- ☐ generate/regenerate per item (оба режима, оригинал/спин).
- ☐ post/repost per item (claim, exclude текущего сайта, +слот).
- ☐ manual create не генерит, создаёт пустые айтемы нужного числа.

### Фаза 2 — streaming-пайплайн (auto) — ОСНОВА ГОТОВА
- ☑ **Унифицировали create**: оба режима (manual/auto) пред-создают пустые айтемы → `READY`, Start ручной. Auto-генерация-на-create убрана. (Проверено живьём: auto gen_per_post 3 строки → 3 пустых, `gen 0/3`, READY.)
- ☑ Start (auto) → `run_posting` (celery). В `_run_posting_async`: для auto csv_campaign с пустыми айтемами запускаем `generate_run_items(finalize=True)` как **параллельную asyncio-корутину в той же task** (не отдельный процесс — проще координация).
- ☑ `_pick_pending_batch(require_text=True)` гейт (Фаза 1) — постинг берёт только готовые.
- ☑ Генерация инкрементальная: `generate_run_items(finalize=True)`:
  - `gen_per_row` → `_gen_group_original` (AI-тело в плейсхолдер, НЕ трогая айтемы) + `_fanout_one_group` (атомарно финализирует группу: оригинал+спины с инжектом ссылки) — постинг не схватит полу-готовую группу. Извлёк `_fanout_one_group` из `_fill_campaign_groups` (переиспользуется и на Start).
  - `gen_per_post` → `generate_item` per айтем (каждый сразу постабелен).
- ☑ Завершение (§2.6, через БД, без shared-флагов): постинг при пустом батче — пока `gen_task` не `done()` → `sleep(2) + continue`; когда генерация завершена и готовых нет → финиш.
- ☑ Пауза/отмена останавливают генерацию (`generate_run_items` проверяет `pause_requested`/`cancel_requested` между айтемами/группами). `finally` постинга await/cancel `gen_task`.

**Тесты:** ☑ `generate_run_items(finalize=True)` gen_per_row → все айтемы финальные + ссылка инжектнута. ☑ text-гейт (Фаза 1).
**Осталось:** ☐ UI — одновременный показ ген+пост прогресса (сейчас очередь переключает: ген-бар пока генерит → пост-бар). ☐ Живая проверка полного стрима (нужны AI-модель + реальный WP-постинг).

### Фаза 3 — drip-aware генерация — ГОТОВА
- ☑ **Фикс роутинга Старта (баг Фазы 2):** auto gen_per_row больше НЕ идёт в `start_campaign_fanout` (он расшивал спины из ПУСТЫХ оригиналов) — auto стримит через `run_posting`. Условие: `csv_campaign + run_mode==manual + deferred_fanout` → fanout; иначе run_posting.
- ☑ **Стаггеринг not_before** для auto-кампаний (`apply_drip_not_before`, random per item, идемпотентно) — вызывается в `run_posting` перед стримом, если `spread_days>0`. (Manual gen_per_row — как было, в `start_campaign_fanout`.)
- ☑ **Генератор горизонт-aware** (`_STREAM_GEN_HORIZON=6ч`): `generate_run_items(finalize=True)` генерит только айтемы с `not_before IS NULL OR <= now+6ч`. На каждый заход (cron-wake) генерит порцию дня; постинг постит «созревшие», перевзводит run в `scheduled` до след. порции (drip re-arm Фазы 2). Так на 30-дневном файле в день 1 генерится/постится только порция дня.
  - gen_per_post: горизонт per-item. gen_per_row: горизонт по оригиналу группы (AI = 1 на строку, дёшево; fanout группы при созревании).
- ☑ Взаимодействие с drip-постингом: общий `not_before`, общий re-arm-cron (`dispatch_scheduled_runs`).

**Тесты:** ☑ `apply_drip_not_before` (окно [now, now+N]). ☑ горизонт: айтем `not_before=+30д` НЕ генерится, due-айтем генерится.

## 5bis. UX
- ☑ **Dual-progress бар** — одна полоска: зелёный = постинг, красный = генерация (сгенерировано, ждёт постинга), серый = ещё не сгенерировано. Подпись «генерация X/Y · постинг A/B». На странице рана (`runs/[id]`) и в Global Queue (`queue`). Бэкенд: `run_progress_counts`/`_posting_lane` отдают `generated` (айтемы с `text_id`); `RunProgressResponse`/`QueuePostingItem`/TS-типы += `generated`. UI поллит `loadProgress` каждые 10с для активного рана (SSE-progress не несёт `generated`). Проверено: `/progress` → `generated:1,total:3,posted:0`.
- ☐ Живая проверка полного auto-стрима + drip (нужны AI-модель + WP-постинг).

## 4. Открытые вопросы (подтвердить)
- ☐ **Под-статус `generating`** у айтема — заводим (рекомендуется) или клейм через флаг?
- ☐ **Repost у gen_per_row спина** — repost именно спина (его текст) на новый сайт; оригинал не трогаем. Ок?
- ☐ **Bulk «Сгенерировать тексты» в manual** — фоновая с отменой (рекоменд.) vs простая блокирующая.
- ☐ **Горизонт drip-генерации** (Фаза 3) — сутки? настраиваемо?
- ☐ Нужен ли **Repost для link** в Фазе 1 или отложить.

## 5. Тестирование (общее)
- Юнит на пер-айтем логику (claim, классификация, exclude-сайт).
- Реальные SQL-гейты (как `test_link_hopping`/`test_run_max_posts_per_site`) — без моков на запросах.
- Фикстуры чистят за собой (commit в общую dev-БД → удалять по уникальному имени).

## 6. Файлы-якоря (reference map)
- Генерация: `domain/content_engine/campaign.py` (`generate_campaign_run`, `_gen`, `_materialize_one`, `_create_group_items`, `_fill_campaign_groups`, `_finalize_run`, `_gen_progress`), `domain/content_engine/service.py` (`make_variant`, `inject_link`).
- Постинг-воркер: `workers/celery/posting.py` (`_run_posting_async`, `_pick_pending_batch`, `_post_one_item`, `_pick_candidate_sites`, drip re-arm).
- Link-воркер (образец self-contained per-item): `domain/wp_links/service.py` (`process_link_item`, `_place_on_site`), `workers/celery/posting.py::_run_link_async`.
- Старт/экшены: `api/admin/routes/postings.py` (`start_run_endpoint`, `restart_run_endpoint`, `text-items/{item_id}/remove-link`).
- Модели/статусы: `infrastructure/db/models/posting.py` (`TextItem`, `TextItemStatus`, `PostingRun`, `PostingRunStatus`).
- UI: `ui/src/routes/(app)/runs/[id]/+page.svelte`, `ui/src/routes/(app)/queue/+page.svelte`, `ui/src/lib/api/admin.ts`, `ui/src/lib/api/types.ts`.
