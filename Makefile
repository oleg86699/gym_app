# gym_app — локальная разработка
#
# Один файл compose с base + dev override. Production-override (docker-compose.deploy.yaml)
# подъедет в этапе 1.5 когда подключим git и серверы.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose -f docker-compose.yaml -f docker-compose.dev.yaml

# Цвета для логов
BLU := \033[1;34m
GRN := \033[1;32m
YEL := \033[1;33m
RED := \033[1;31m
NC  := \033[0m

# ──────────────────────────────────────────────────────────────────────
# Хост: подготовка папок персистентных данных
# ──────────────────────────────────────────────────────────────────────

.PHONY: init-host
init-host: ## Создать ./data и ./backups с правильными правами
	@echo -e "$(BLU)[init-host]$(NC) creating persistent data folders"
	@bash deploy/init-host.sh

# ──────────────────────────────────────────────────────────────────────
# Стек
# ──────────────────────────────────────────────────────────────────────

.PHONY: up
up: _check-env _check-data ## Поднять стек в фоне
	@echo -e "$(BLU)[up]$(NC) starting stack"
	$(COMPOSE) up -d --build
	@echo -e "$(GRN)[up]$(NC) waiting for healthchecks..."
	@sleep 3
	@$(MAKE) -s ps

.PHONY: down
down: ## Остановить стек. Данные сохраняются.
	@echo -e "$(BLU)[down]$(NC) stopping stack (data preserved in ./data/)"
	$(COMPOSE) down

.PHONY: restart
restart: down up ## Перезапуск стека

.PHONY: rebuild
rebuild: ## Пересобрать образы и поднять
	$(COMPOSE) build --pull
	@$(MAKE) -s up

.PHONY: ps
ps: ## Список сервисов и их статусы
	$(COMPOSE) ps

.PHONY: logs
logs: ## Тейл логов всех сервисов
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-app
logs-app: ## Тейл логов backend
	$(COMPOSE) logs -f --tail=200 app

.PHONY: logs-ui
logs-ui: ## Тейл логов UI
	$(COMPOSE) logs -f --tail=200 ui

.PHONY: logs-db
logs-db: ## Тейл логов postgres
	$(COMPOSE) logs -f --tail=200 db

# ──────────────────────────────────────────────────────────────────────
# Шеллы внутрь контейнеров
# ──────────────────────────────────────────────────────────────────────

.PHONY: shell-app
shell-app: ## Bash в контейнере backend
	$(COMPOSE) exec app bash

.PHONY: shell-db
shell-db: ## psql в контейнере postgres
	$(COMPOSE) exec db psql -U $${POSTGRES_USER} $${POSTGRES_DB}

.PHONY: shell-redis
shell-redis: ## redis-cli
	$(COMPOSE) exec redis redis-cli

# ──────────────────────────────────────────────────────────────────────
# Миграции / тесты / линт (наполнится по мере этапов)
# ──────────────────────────────────────────────────────────────────────

.PHONY: migrate
migrate: ## Накатить миграции Alembic
	$(COMPOSE) exec app alembic upgrade head

.PHONY: migrate-new
migrate-new: ## Создать новую миграцию: make migrate-new name=add_users
	@test -n "$(name)" || (echo "Usage: make migrate-new name=description" && exit 1)
	$(COMPOSE) exec app alembic revision --autogenerate -m "$(name)"

.PHONY: migrate-down
migrate-down: ## Откатить одну миграцию (downgrade -1)
	$(COMPOSE) exec app alembic downgrade -1

.PHONY: seed
seed: ## Создать super_admin + системные роли/страницы/permissions (idempotent)
	$(COMPOSE) exec app python -m scripts.seed

.PHONY: reset-super-admin-password
reset-super-admin-password: ## Сбросить пароль super_admin на текущий SUPER_ADMIN_PASSWORD из .env
	@echo -e "$(YEL)[reset]$(NC) recreating app to reload .env..."
	$(COMPOSE) up -d --force-recreate app
	@sleep 6
	$(COMPOSE) exec app python -m scripts.reset_super_admin_password

.PHONY: test
test: ## Прогнать pytest и vitest
	@echo -e "$(BLU)[test]$(NC) backend pytest..."
	$(COMPOSE) exec app pytest tests/ || true
	@echo -e "$(BLU)[test]$(NC) ui vitest..."
	$(COMPOSE) exec ui npm run test || true

.PHONY: lint
lint: ## ruff + prettier check
	@echo -e "$(BLU)[lint]$(NC) ruff..."
	$(COMPOSE) exec app ruff check src/ || true
	$(COMPOSE) exec app ruff format --check src/ || true
	@echo -e "$(BLU)[lint]$(NC) prettier..."
	$(COMPOSE) exec ui npx prettier --check src/ || true

.PHONY: fmt
fmt: ## ruff format + prettier --write
	$(COMPOSE) exec app ruff format src/ || true
	$(COMPOSE) exec app ruff check --fix src/ || true
	$(COMPOSE) exec ui npx prettier --write src/ || true

# ──────────────────────────────────────────────────────────────────────
# Бэкапы и обслуживание
# ──────────────────────────────────────────────────────────────────────

.PHONY: backup-now
backup-now: ## Запустить бэкап прямо сейчас (вне расписания)
	$(COMPOSE) exec backup /usr/local/bin/run.sh

.PHONY: prune
prune: ## Почистить ненужные docker-образы. НЕ трогает данные.
	$(COMPOSE) exec backup /usr/local/bin/prune.sh

.PHONY: reset-db
reset-db: ## Дроп + создание чистой БД. Файлы ./data/postgres сохраняются.
	@read -p "$$(echo -e $(RED)Reset DB?$(NC) Это удалит ВСЕ данные приложения. y/N: )" yn; \
	if [[ ! "$$yn" =~ ^[Yy]$$ ]]; then echo "aborted"; exit 1; fi
	$(COMPOSE) exec db dropdb -U $${POSTGRES_USER} $${POSTGRES_DB} --if-exists
	$(COMPOSE) exec db createdb -U $${POSTGRES_USER} $${POSTGRES_DB}
	@$(MAKE) -s migrate
	@$(MAKE) -s seed

# ──────────────────────────────────────────────────────────────────────
# Внутренние проверки
# ──────────────────────────────────────────────────────────────────────

.PHONY: _check-env
_check-env:
	@test -f .env || (echo -e "$(RED)[error]$(NC) .env not found. Run: cp .env.example .env && edit it" && exit 1)

.PHONY: _check-data
_check-data:
	@test -d ./data/postgres || (echo -e "$(RED)[error]$(NC) ./data/postgres not found. Run: make init-host" && exit 1)

# ──────────────────────────────────────────────────────────────────────
# Help
# ──────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Показать эту справку
	@echo -e "$(BLU)gym_app — Makefile targets$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GRN)%-18s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo -e "$(YEL)Persistent data в ./data/ и ./backups/ — никогда не удаляется автоматически (ADR-014)$(NC)"
