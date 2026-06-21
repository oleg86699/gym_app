# CONTRIBUTING

## Бренчевание

Активируется после подключения git (этап 1.5). Сейчас работаем локально.

Когда подключим git:

- `main` — продакшен. Мерж только из `develop` через PR.
- `develop` — dev. Мерж из feature-веток через PR.
- Feature-ветки: `feat/...`, `fix/...`, `chore/...`, `refactor/...`,
  `docs/...`, `test/...`, `ci/...`.

## Conventional Commits

Title PR и каждый коммит:

```
<type>(<scope>): <subject>
```

Типы: `feat`, `fix`, `chore`, `docs`, `style`, `refactor`, `perf`,
`test`, `build`, `ci`, `revert`.

Брейкинг: `feat!: ...` или `BREAKING CHANGE:` в теле — bump major.

Влияет на semver-расчёт в deploy workflow.

## Чек-листы перед merge PR

### Performance (см. ADR-011, категория А)

PR, добавляющий новый эндпоинт или запрос к БД:

- [ ] `EXPLAIN ANALYZE` нового запроса показывает `Index Scan` на
      больших таблицах, не `Seq Scan`.
- [ ] `actual time` < 100 мс для list-эндпоинтов.
- [ ] List-эндпоинт принимает `cursor` и `limit`.
- [ ] Тяжёлые экспорты возвращают `StreamingResponse`.
- [ ] Нет `await session.execute(...)` в цикле — массовые операции
      через `bulk_insert_mappings` или `execute(insert(...).values([...]))`.

### Docker compose (см. ADR-014)

PR, меняющий `docker-compose*.yaml`:

- [ ] Не появилось новых **named volumes** (только `./path:/container/path`).
- [ ] Не удалены существующие bind mounts.
- [ ] Не добавлен `down -v` в скрипты.
- [ ] Если новый сервис хранит данные — добавлена строка в
      `deploy/PERSISTENT_PATHS.md`.

CI прогоняет `deploy/maintenance/compose-lint.sh` который грепает на
антипаттерны.

### Безопасность

- [ ] Нет секретов в коде (detect-secrets baseline актуален).
- [ ] Пароли/токены — через `passlib` / `Fernet`, не plaintext.
- [ ] SQL — через SQLAlchemy ORM или параметризованные запросы.
      `.format()` / f-string в SQL = блокер.
