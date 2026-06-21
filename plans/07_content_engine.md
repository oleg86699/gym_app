# Этап 7 (proposal). Content Engine: генерация / спин / reuse как режимы прогона

> Статус: **черновик на согласование**. Объединяет логику `gym_gen_content_casino_new`
> (генерация отдельным потоком, спин, reuse) с нашим конвейером `run → text_items →
> постинг`. Цель — один прогон с «режимом источника контента», без зоопарка кнопок/таблиц.

## Ключевая идея
Прогон (`posting_runs`) получает **режим источника контента** + **auto/manual**.
Всё остальное (постинг, drip, scheduled start, proxy, method, аналитика,
needs_review) — переиспользуется как есть. Меняется только КТО и КОГДА наполняет
тело `texts` и сколько создаётся `text_items`.

```
content_source:           как пришёл вход
  upload_txt   — архив .txt (как сейчас)
  csv_direct   — csv: link,anchor,text (готовые тексты)
  csv_campaign — csv: anchor,link,count,keyword[,content_parametrs] (НЕ тексты → генерация/reuse)

content_mode (только для csv_campaign):
  gen_per_row     — 1 оригинал на строку → count размещений через спин оригинала   (режим 1)
  gen_per_post    — уникальный текст на каждое из count размещений                 (режим 2)
  reuse           — берём готовые reusable-тексты из библиотеки + спин + инжект     (режим 3)

run_mode:
  auto   — сгенерили/нашли → подготовили → постинг стартует сам
  manual — сгенерили/нашли → постинг ждёт ручного старта (+ ревью оригиналов)
```

---

## 1. Данные

### Расширяем существующее
- **`texts`** + `spin_formula TEXT NULL` · `reusable BOOL DEFAULT false` ·
  (`times_used` уже есть = счётчик использований спина) · `used_as_original BOOL`.
- **`posting_runs`** + `content_source` · `content_mode` · `run_mode` ·
  `gen_params JSONB` (prompt_template_id, ai_model_id, language, default_keyword,
  reuse_limit_override…).
- **`text_items`** + статусы `awaiting_generation` · `awaiting_review`
  (в постинг не идут, как `needs_review`). Остальное (link/anchor/target_domain/
  not_before/site/post_id) уже есть.

### Новые таблицы (минимум, по образцу `gym_gen_content_casino_new` + скринов)
- **`ai_providers`** — id, name, type(`openai|anthropic|google`), api_key(шифр.),
  base_url(опц.), is_active. (как страница Proxies)
- **`ai_models`** — id, provider_id, display_name, model_id, temperature,
  max_tokens, purpose(`content|spin|any`), is_active.
- **`prompt_templates`** — id, name, body (с `{переменными}`: `{keyword}`,
  `{anchor}`, `{links}`, `{language}`, `{brand}`…), notes. (= их `prompt_gen_content`)

### Настройки (app_settings)
- `max_spin_reuse` — потолок переиспользований спина одного текста (дефолт **50**;
  reuse не берёт тексты с `times_used >= max`).

### Модель хранения тел: MATERIALIZE (решено)
Каждое размещение = **своя строка `texts`** с готовым телом (расшивка спина +
вставленная ссылка), `text_items.text_id` → эта строка. Причина: нужно
проверять подготовленные тексты (manual) и экспортировать «постированную версию»
— а это возможно только если тело **сохранено**.
- `source` различает: `human` · `generated` (оригинал, reusable, со спином) ·
  `spin_variant` (производная от оригинала, `reusable=false`) · `reused`.
- `texts.parent_text_id` (NULL для оригиналов) — производная знает свой оригинал
  (для аналитики/чистки).
- Плата — больше строк → закрываем `archived_at`-чисткой и партиционированием
  `texts` на этапе масштабирования.

---

## 2. Определение входа (форма создания рана)
Один селектор «Источник контента». Если загружают **csv** — по заголовкам:
- `link,anchor,text` → **csv_direct** (готовые тексты);
- `anchor,link,count,keyword…` → **csv_campaign** → показываем выбор
  `content_mode` (1/2/3) + `run_mode` (auto/manual) + язык + шаблон промпта + модель.

Архив .txt / csv_direct по умолчанию → **не reusable**. Чекбокс **«разрешить
reuse»** → текстам сгенерим `spin_formula` (спин-воркер) и пометим `reusable=true`.

---

## 3. Три режима генерации — что куда пишется

### Режим 1 — `gen_per_row` (оригинал + спин на count)
На строку CSV: генерим **1 оригинал** → `texts`(source=generated, reusable=true).
Создаём **count** `text_items` (text_id = этот оригинал):
- 1-я задача = оригинал как есть;
- остальные = `spin(spin_formula)` оригинала + `inject_link(link,anchor)`.
`times_used` оригинала растёт сразу (спин использован на каждый count). Дёшево по
AI (1 генерация на строку), уникальность — за счёт спина.

### Режим 2 — `gen_per_post` (уникальный на каждый пост)
На строку CSV с count=N: создаём **N** `text_items` и под **каждую** генерим
**уникальный** текст → **N** строк `texts`(generated, reusable=true). Дороже по AI,
максимальная уникальность. Каждый текст потом доступен под reuse (счётчик спина).

### Режим 3 — `reuse`
Создаём `text_items` на каждый count → под каждую **ищем** в библиотеке
подходящий текст:
```
reusable=true AND spin_formula IS NOT NULL AND times_used < max_spin_reuse
AND archived_at IS NULL  [+ фильтр по языку/теме через FTS]
```
→ `spin()` + `inject_link` → постим; `times_used++` у источника. **Залитые
1000-из-10-спинов (`human`, без спина) сюда НЕ попадают** — твоё правило.

---

## 4. Auto vs Manual
- **Auto:** сгенерили/нашли всё → `text_items`+`texts` заполнены → постинг сам.
- **Manual:** генерим/находим, постинг НЕ стартует. Для режима 1 — генерим
  **только оригиналы** (по 1 на строку), человек **проверяет/правит** их в нашем
  редакторе. На «Старт»: делаем `spin_formula` оригиналов → применяем к оставшимся
  `text_items`/`texts` (count был, напр., 100 → 100 строк) → постим. Это
  обобщённый «ревью до спина».

---

## 5. Генерация и спин — ОТДЕЛЬНЫЕ полосы (не синхронно с постингом)
Как в `gym_gen_content_casino_new` (`gen_content_worker` + `gen_spin_worker`) и как
договаривались в балансировщике:
- **gen-воркер**: берёт `text_items`/строки в `awaiting_generation` → провайдер+
  промпт+модель → пишет `texts.body` → флипает `pending`.
- **spin-воркер**: заполняет `spin_formula` где надо (reuse/manual-старт).
- Обе — свои полосы в Global Queue, со своими лимитерами (LLM-rate). Постинг от них
  не зависит.

Контракт чистый: воркеры читают «что нужно сгенерить/спинить» из БД, пишут в
`texts`, двигают статусы. Сам **AI-вызов** (SDK провайдера) — по `ai_providers`/
`ai_models` + `prompt_templates` (с `_safe_format` подстановкой переменных задачи).

## 6. Спин-расшивка и инжект — чистый код (без AI)
- `spin(formula)` — детерминированный разворот `{a|b|c}` (как `spintax.spin`).
- `inject_link(body, link, anchor)` — уже есть.
- **Когда**: при подготовке (auto) или на ручном старте (manual) — разворачиваем
  спин в готовые тела и **сохраняем** (materialize, §1), затем постим. НЕ при
  каждом постинге.
AI нужен только для: генерации тела и (опц.) превращения текста в спинтакс.

---

## 7. AI-провайдеры / модели / промпты (UI как Proxies)
- Страница **AI Providers** (ключ + тип + base_url) — как Proxies (скрин 1-2).
- **Model Configurations** (provider, model_id, temp, max_tokens, purpose) —
  (скрин 3).
- **Prompt Templates** — список (name + body с `{переменными}`), выбор при создании
  кампании (скрин 4). Данные задачи подставляются при генерации.

## 8. Интеграция с тем, что есть
Scheduled start, **drip (N дней)**, proxy pool, posting method, priority, needs_review,
аналитика по target_domain — **всё применяется поверх** любого режима. Кампания
просто наполняет `text_items`, дальше — общий конвейер.

## 9. Экспорт
- Кнопку выгрузки рана обновить → **все данные рана + тексты** (CSV: link, anchor,
  target_domain, site, post_id, status, body/snippet, lang, source…).
- Добавить **экспорт текстов рана в .txt архивом** (1 txt = 1 текст).

---

## 10. Фазирование
- **C1 — каркас контента (без AI):** расширения `texts`/`runs`/`text_items` +
  статусы; spin-расшивка (детерминир.) + fanout + inject; csv_campaign парсер;
  ручной spin_fanout (режим 1 manual) на готовом/вставленном тексте; экспорт.
- **C2 — AI-инфра:** `ai_providers`/`ai_models`/`prompt_templates` + страницы +
  gen-воркер + spin-воркер (полосы). Подключаем генерацию к режимам 1/2.
- **C3 — reuse:** пикер по `reusable+spin+limit`, счётчики, настройка `max_spin_reuse`.

(C1 даёт рабочий ручной инструмент сразу; C2 включает авто-генерацию; C3 — reuse.)

## 11. Решения (согласовано)
1. ✅ **AI-вызов строю я**: шаблон промпта + подстановка переменных из строки CSV
   (как `_safe_format`) + отправка провайдеру; + управление промптами; язык из
   `texts.lang`. (Это и был «генератор» — просто не сразу.)
2. ✅ **`max_spin_reuse = 50`** (дефолт, настраивается).
3. ✅ **Manual**: mode 1 — ревью только оригиналов (по 1 на строку), на старте
   спин→заполнение остальных; mode 2 — генерим уникальные на каждую строку, постинг
   по ручному старту; mode 3 — находим+заполняем сразу, постинг по ручному старту.
4. ✅ **MATERIALIZE**: каждый `text_item` → своя `texts`-строка с готовым телом
   (см. «Модель хранения тел» в §1). Следствие #3 и #5.
5. ✅ **Экспорт .txt = постированная версия** (тело `texts`-строки этого
   размещения, последнее в БД).
