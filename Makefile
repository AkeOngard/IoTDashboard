# Single entry point for the stack (doc §12).
# Targets that need the security phase (create-admin, reset-admin-2fa) are not
# here yet -- they arrive with the users table.

COMPOSE      := docker compose
COMPOSE_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml
PY           := python

.DEFAULT_GOAL := help
.PHONY: help init up down restart logs ps build dev \
        prod-up prod-deploy prod-logs migrate migrate-status migrate-verify migrate-new \
        health ready commission matter-nodes backup restore tunnel clean

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------- lifecycle

init: ## create .env with random secrets (never overwrites an existing one)
	@if [ -f .env ]; then \
	  echo ".env already exists -- leaving it alone"; \
	else \
	  cp .env.example .env && $(PY) scripts/init_env.py && chmod 600 .env; \
	  echo "now set PUBLIC_ORIGIN and, if using it, CF_TUNNEL_TOKEN in .env"; \
	fi

build: ## build the app image
	$(COMPOSE) build

up: ## DEV: build, migrate and start (loads docker-compose.override.yml)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory health

down: ## stop the stack
	$(COMPOSE) down

restart: ## restart the app only
	$(COMPOSE) restart app

ps: ## show container status
	$(COMPOSE) ps

logs: ## tail logs; pick a service with: make logs s=app
	$(COMPOSE) logs -f --tail=200 $(s)

dev: ## run the app locally with reload (no containers)
	$(PY) -m uvicorn app.main:app --reload --port $${PORT:-8000}

# ---------------------------------------------------------------- migrations

migrate: ## apply pending migrations
	$(PY) scripts/migrate.py up

migrate-status: ## show applied / pending migrations
	$(PY) scripts/migrate.py status

migrate-verify: ## checksum-verify applied migrations
	$(PY) scripts/migrate.py verify

migrate-new: ## scaffold a migration: make migrate-new name=add_scenes
	$(PY) scripts/migrate.py new $(name)

# -------------------------------------------------------------------- matter

commission: ## join a device to the fabric: make commission code=MT:Y.K90...
	@test -n "$(code)" || (echo "usage: make commission code=<pairing-code>" && exit 1)
	$(COMPOSE_PROD) exec app python scripts/commission.py $(code)

matter-nodes: ## dump endpoints/clusters the bridge exposes
	$(COMPOSE_PROD) exec app python scripts/dump_nodes.py

# --------------------------------------------------------------------- ops

health: ## print /healthz
	@curl -fsS http://127.0.0.1:$${PORT:-8000}/healthz | $(PY) -m json.tool

ready: ## readiness probe (exit 1 when not ready)
	@curl -fsS http://127.0.0.1:$${PORT:-8000}/readyz | $(PY) -m json.tool

backup: ## full pg_dump into ops/backups (uses the db container's own pg_dump)
	PG_EXEC="docker exec -i iot-db" bash scripts/backup.sh

restore: ## restore into an EMPTY db: make restore file=ops/backups/iot-<stamp>.dump
	@test -n "$(file)" || (echo "usage: make restore file=<dump>" && exit 1)
	PG_EXEC="docker exec -i iot-db" bash scripts/restore.sh $(file)

tunnel: ## start the Cloudflare tunnel profile
	$(COMPOSE) --profile tunnel up -d cloudflared

prod-up: ## PROD: first bring-up with the prod overlay (ignores the dev override)
	$(COMPOSE_PROD) up -d --build
	@$(MAKE) --no-print-directory health

prod-logs: ## tail prod logs; pick a service with: make prod-logs s=app
	$(COMPOSE_PROD) logs -f --tail=200 $(s)

prod-deploy: ## rolling update with the prod overlay
	git pull --ff-only
	$(COMPOSE_PROD) build app
	$(COMPOSE_PROD) run --rm migrate
	$(COMPOSE_PROD) up -d --no-deps app
	@$(MAKE) --no-print-directory health

clean: ## remove containers and volumes (DESTROYS telemetry)
	$(COMPOSE) down -v
