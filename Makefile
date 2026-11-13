# ---------------------------------------------------------------------------
# Gaia developer tasks.
#   make help          list targets
#   make up            start metadata DB + Redis
#   make check-target  verify the read-only target connection
# ---------------------------------------------------------------------------

SHELL := /bin/bash
ROOT := $(shell pwd)
VENV := $(ROOT)/gaia-env
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- stack -----------------------------------------------------------------

.PHONY: up
up: ## Start metadata DB and Redis
	$(COMPOSE) up -d
	@$(COMPOSE) ps

.PHONY: down
down: ## Stop the stack (keeps data)
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=100

.PHONY: reset-db
reset-db: ## DESTROY metadata volume and re-initialise from init scripts
	@read -p "This deletes the Gaia metadata database. Continue? [y/N] " a; \
	  [[ "$$a" == "y" ]] || { echo "aborted"; exit 1; }
	$(COMPOSE) down -v
	$(COMPOSE) up -d
	@echo "Waiting for postgres..."; sleep 5
	$(MAKE) verify-extensions

.PHONY: verify-extensions
verify-extensions: ## Confirm required extensions are installed
	@docker exec gaia-metadata-db psql -U $${GAIA_METADATA_USER:-gaia} -d $${GAIA_METADATA_DB:-gaia} \
	  -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;"

.PHONY: psql
psql: ## Open a psql shell on the metadata DB
	@docker exec -it gaia-metadata-db psql -U $${GAIA_METADATA_USER:-gaia} -d $${GAIA_METADATA_DB:-gaia}

.PHONY: redis-cli
redis-cli: ## Open a redis-cli shell
	@docker exec -it gaia-redis redis-cli

# --- python ----------------------------------------------------------------

.PHONY: venv
venv: ## Install backend dependencies into gaia-env
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -e $(ROOT)/backend
	$(PIP) install -e "$(ROOT)/backend[dev]"

.PHONY: check-env
check-env: ## Verify gaia-env imports the dependency stack
	$(PY) -c "import fastapi, sqlalchemy, asyncpg, pyodbc, httpx, sqlglot, redis, structlog, pgvector; print('deps ok')"
	$(PY) -c "import pyodbc; print('ODBC drivers:', pyodbc.drivers())"

.PHONY: dev-api
dev-api: ## Run the FastAPI app with reload
	cd $(ROOT)/backend && $(VENV)/bin/uvicorn gaia.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: test
test: ## Run the test suite
	cd $(ROOT)/backend && $(VENV)/bin/pytest -q

.PHONY: lint
lint: ## Ruff check + format check
	cd $(ROOT)/backend && $(VENV)/bin/ruff check src

# --- target database -------------------------------------------------------

.PHONY: check-target
check-target: ## Verify the read-only SQL Server connection
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_target

.PHONY: target-info
target-info: ## Print a catalog summary of the target database
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_target --summary

# --- embeddings ------------------------------------------------------------

.PHONY: pull-embed
pull-embed: ## Pull the embedding model into the Ollama container
	docker exec ollama-code ollama pull $${GAIA_EMBEDDINGS_MODEL:-bge-m3}

.PHONY: introspect
introspect: ## Run a catalog introspection pass (read, diff, persist)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect

.PHONY: introspect-dry
introspect-dry: ## Introspection without persisting
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect --dry-run

.PHONY: introspect-json
introspect-json: ## Dump the canonical snapshot document
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect --dry-run --json /tmp/gaia-snapshot.json

.PHONY: check-embed
check-embed: ## Smoke-test the embedding endpoint
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_embeddings

.PHONY: check-llm
check-llm: ## Smoke-test the reasoning LLM (plain, JSON mode, thinking)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_llm

# --- frontend --------------------------------------------------------------

.PHONY: dev-web
dev-web: ## Run the Vite dev server
	cd $(ROOT)/frontend && pnpm dev
