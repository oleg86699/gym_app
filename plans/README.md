# Планы по проекту

Это набор живых документов, к которым мы возвращаемся между этапами.
Каждый файл — отдельная веха.

## Порядок чтения

1. [`00_overview.md`](00_overview.md) — стек, архитектура, структура
   репозитория, оглавление этапов.
2. [`architecture_decisions.md`](architecture_decisions.md) — ADR-ы по
   ключевым техническим решениям (очереди, хранение файлов, RBAC,
   индексы, real-time). Читать прежде всего.
3. [`01_stage1_foundation.md`](01_stage1_foundation.md) — этап 1:
   docker, git, FastAPI, SvelteKit, RBAC, пользователи.
4. [`02_stage2_posting_core.md`](02_stage2_posting_core.md) — этап 2:
   ядро постинга через XML-RPC.
5. [`03_stage3_dashboards_monitoring.md`](03_stage3_dashboards_monitoring.md) —
   этап 3: real-time, дешборды, валидатор, прокси, нотификации.
6. [`04_stage4_developer_api.md`](04_stage4_developer_api.md) — этап 4:
   публичное программное API + документация.
7. [`05_stage5_roadmap_extras.md`](05_stage5_roadmap_extras.md) — этап 5:
   Indexing API, Spintax, AI, главные страницы WP, накопленный
   тех-долг.
8. [`06_text_library_proposal.md`](06_text_library_proposal.md) — этап 6
   (**черновик на согласование**): унифицированная библиотека текстов,
   разбор ссылок/анкоров из текстов, `project_domains`, disambiguation +
   needs_review, язык, поиск (FTS + trgm + опц. pgvector), аналитика по
   доменам. Фаза A (сейчас) + Фаза B (с генератором).
9. [`07_content_engine.md`](07_content_engine.md) — этап 7
   (**черновик на согласование**): генерация/спин/reuse как режимы прогона
   (content_source + content_mode + auto/manual), AI-провайдеры/модели/
   промпты (UI как Proxies), отдельные полосы gen/spin воркеров, экспорт
   рана+текстов. Опирается на логику `gym_gen_content_casino_new`.

## Где живёт код

Корень нового проекта — `/Volumes/profile/github/gym/app/` (создаётся в
рамках первой ветки этапа 1).

Старый Flask-проект — `/Volumes/profile/github/gym/flask/` — архив, не
правим, не используем.

Референс модели server_app — `/Volumes/profile/github/server_app/` —
оттуда подсматриваем зрелые куски (CI/CD workflow, RBAC модели,
docker-compose layout), но не копируем как есть.

## Как меняем планы

- Открытые вопросы по этапу — секция «Открытые вопросы» в конце каждого
  плана. Закрываем по мере уточнения, фиксируем ответ прямо в документе.
- Изменение архитектурного решения — новая запись в
  `architecture_decisions.md` со статусом `superseded` для старой.
- Перенос задач между этапами — двигаем подзадачу руками, оставляем
  пометку в новом этапе откуда взяли.

## Дальше

После прочтения и принятия плана — стартуем этап 1, ветка
`chore/bootstrap-repo`.
