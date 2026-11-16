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
# Optional target selection for target-facing commands, e.g. TARGET=demo_pg
TARGET_ARG := $(if $(TARGET),--target $(TARGET),)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Taiga: Cleanup --------------------------------------------------------
.PHONY: cleanup
cleanup:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "Cleaned up pycache and pytest cache files for Taiga upload."

.PHONY: strip-verification
strip-verification: ## Delete the first line (captured local `cd` echo) of verification outputs
	@bash $(ROOT)/scripts/strip_verification_first_line.sh $(ARGS)

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
	$(PY) -c "import fastapi, sqlalchemy, asyncpg, psycopg, pyodbc, httpx, sqlglot, redis, structlog, pgvector; print('deps ok')"
	$(PY) -c "import pyodbc; print('ODBC drivers:', pyodbc.drivers())"

.PHONY: dev-api
dev-api: ## Run the FastAPI app with reload
	cd $(ROOT)/backend && $(VENV)/bin/uvicorn gaia.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: stamp
stamp: ## Print which code this is (first line of every verification output)
	@cd $(ROOT)/backend && $(PY) -c "import gaia, sqlglot; print('gaia build', gaia.BUILD, '· sqlglot', sqlglot.__version__)" ; \
	  git -C $(ROOT) log -1 --format='git %h %cs' 2>/dev/null || true

.PHONY: test
test: ## Run the test suite (ARGS="tests/integration/test_x.py -k name" for a subset)
	cd $(ROOT) && $(VENV)/bin/pytest -q -c backend/pyproject.toml $(ARGS)

.PHONY: test-db
test-db: ## Create the disposable gaia_test database used by the PostgreSQL integration tests
	-docker exec gaia-metadata-db createdb -U $${GAIA_METADATA_USER:-gaia} gaia_test
	@echo "then: GAIA_TEST_DATABASE_URL=postgresql+asyncpg://gaia:<password>@127.0.0.1:5433/gaia_test make test"

.PHONY: lint
lint: ## Ruff check + format check
	cd $(ROOT)/backend && $(VENV)/bin/ruff check src

# --- target database -------------------------------------------------------

.PHONY: master-key
master-key: ## 4F: print a new GAIA_MASTER_KEY (encrypts data-source credentials; keep it in .env)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.datasource new-key

.PHONY: datasource
datasource: ## 4F: data sources added at runtime (ARGS="list | add … | probe NAME | update | disable | enable | remove | rotate-key")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.datasource $(ARGS)

.PHONY: check-target
check-target: ## Verify the read-only target connection (TARGET=demo_pg for PostgreSQL)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_target $(TARGET_ARG)

.PHONY: target-info
target-info: ## Print a catalog summary of the target database (TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_target --summary $(TARGET_ARG)

# --- PostgreSQL development target (D25) -------------------------------------

.PHONY: pg-target-up
pg-target-up: ## Start the PostgreSQL development target (127.0.0.1:5434)
	$(COMPOSE) --profile target-pg up -d gaia-target-pg
	@$(COMPOSE) --profile target-pg ps gaia-target-pg

.PHONY: pg-target-seed
pg-target-seed: ## (Re)create and populate the PostgreSQL target (ARGS="--scale 1 --seed 42")
	$(PY) $(ROOT)/infra/pg-target/seed.py $(ARGS)

.PHONY: pg-target-psql
pg-target-psql: ## psql on the PostgreSQL target as the read-only role
	@docker exec -it gaia-target-pg psql -U $${GAIA_PG_TARGET_USER:-gaia_ro} -d $${GAIA_PG_TARGET_DATABASE:-gaia_target_pg} -h 127.0.0.1

.PHONY: pg-target-down
pg-target-down: ## Stop the PostgreSQL target (keeps data)
	$(COMPOSE) --profile target-pg stop gaia-target-pg

.PHONY: pg-target-reset
pg-target-reset: ## DESTROY the PostgreSQL target volume
	@read -p "This deletes the PostgreSQL development target. Continue? [y/N] " a; \
	  [[ "$$a" == "y" ]] || { echo "aborted"; exit 1; }
	$(COMPOSE) --profile target-pg rm -sf gaia-target-pg
	docker volume rm gaia-target-pgdata

# --- embeddings ------------------------------------------------------------

.PHONY: pull-embed
pull-embed: ## Pull the embedding model into the Ollama container
	docker exec ollama-code ollama pull $${GAIA_EMBEDDINGS_MODEL:-bge-m3}

.PHONY: introspect
introspect: ## Run a catalog introspection pass (read, diff, persist; TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect $(TARGET_ARG)

.PHONY: introspect-dry
introspect-dry: ## Introspection without persisting (TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect --dry-run $(TARGET_ARG)

.PHONY: introspect-json
introspect-json: ## Dump the canonical snapshot document (TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.introspect --dry-run --json /tmp/gaia-snapshot.json $(TARGET_ARG)

.PHONY: measure
measure: ## Phase 3.0 profiling-cost measurements (TARGET=...; ARGS="--table dw.sales_wide")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.measure_target $(TARGET_ARG) $(ARGS)

.PHONY: profile
profile: ## Phase 3A Stage 1: profile the target + join candidates (TARGET=...; ARGS="--table x --report")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.profile $(TARGET_ARG) $(ARGS)

.PHONY: profile-dry
profile-dry: ## What Stage 1 would profile, reuse or skip; no target reads (TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.profile --dry-run $(TARGET_ARG) $(ARGS)

.PHONY: profile-tests
profile-tests: ## Phase 3A Stage 3: test the stored claims (after make reason; TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.profile --stage 3 $(TARGET_ARG) $(ARGS)

.PHONY: check-embed
check-embed: ## Smoke-test the embedding endpoint
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_embeddings

.PHONY: reason
reason: ## Reasoning over the stored snapshot and evidence (ARGS="--table dbo.x --with-neighbours"; TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.reason $(TARGET_ARG) $(ARGS)

.PHONY: reason-dry
reason-dry: ## Show what reasoning would do; no LLM calls, no writes (ARGS=...; TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.reason --dry-run $(TARGET_ARG) $(ARGS)

.PHONY: rescore
rescore: ## Score stored answers against today's evidence; no LLM calls (ARGS=...; TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.reason --rescore $(TARGET_ARG) $(ARGS)

# --- Phase 3C evaluation -------------------------------------------------------

.PHONY: evaluate-plan
evaluate-plan: ## 3C: scope, conditions, call estimate; no model calls (ARGS="--table dbo.x --with-neighbours")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics plan $(TARGET_ARG) $(ARGS)

.PHONY: evaluate-labels
evaluate-labels: ## 3C: the owner's 40-column labelling template (ARGS="--file F --table ...")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics labels $(TARGET_ARG) $(ARGS)

.PHONY: evaluate
evaluate: ## 3C: run conditions, resumable (ARGS="--out DIR --table ... [--condition B-P]")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics run $(TARGET_ARG) $(ARGS)

.PHONY: evaluate-freeze
evaluate-freeze: ## 3C: freeze a run's snapshot and evidence (no model; then profiling may change)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics freeze $(TARGET_ARG) $(ARGS)

.PHONY: evaluate-judge
evaluate-judge: ## 3C: judge the answers (ARGS="--out DIR --labels F")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics judge $(ARGS)

.PHONY: evaluate-audit
evaluate-audit: ## 3C: the owner's 20 % audit sample (ARGS="--out DIR")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics audit $(ARGS)

.PHONY: evaluate-report
evaluate-report: ## 3C: metrics and criteria (ARGS="--out DIR --labels F")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics report $(ARGS)

.PHONY: evaluate-cost
evaluate-cost: ## 3C criterion 5: production run cost per table (TARGET=...; ARGS="--file F")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.evaluate_semantics cost $(TARGET_ARG) $(ARGS)

# --- Phase 4: queries --------------------------------------------------------------

.PHONY: query
query: ## 4A: validate + run one statement through the SQL guard (TARGET=...; ARGS="--sql '...'")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.query $(TARGET_ARG) $(ARGS)

.PHONY: retrieve
retrieve: ## 4B: the schema context for one question (TARGET=...; ARGS="--question '...' [--context]")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.retrieve ask $(TARGET_ARG) $(ARGS)

.PHONY: retrieval-eval
retrieval-eval: ## 4B: recall of a question set (TARGET=...; ARGS="--file F [--k 5] [--no-vectors]")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.retrieve eval $(TARGET_ARG) $(ARGS)

.PHONY: ask
ask: ## 4C: ask a business question (TARGET=...; ARGS="--question '...' [--clarified '...'] [--show-sql]")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.ask $(TARGET_ARG) $(ARGS)

.PHONY: golden
golden: ## 4D: run a golden set (TARGET=...; ARGS="--file F --out DIR [--only ids] [--replay DIR]")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.golden $(TARGET_ARG) $(ARGS)

.PHONY: clarifications
clarifications: ## D61: clarifications asked and how often Gaia's choice was the user's (TARGET=...)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.clarifications $(TARGET_ARG) $(ARGS)

.PHONY: query-check
query-check: ## 4A: the SQL guard only; nothing executed or recorded (TARGET=...; ARGS="--sql '...'")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.query --dry-run $(TARGET_ARG) $(ARGS)

.PHONY: review
review: ## Show the SME review queue, highest priority first (TARGET=...; ARGS="--limit 100")
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.review $(TARGET_ARG) $(ARGS)

.PHONY: check-llm
check-llm: ## Smoke-test the reasoning LLM (plain, JSON mode, thinking)
	@cd $(ROOT)/backend && $(PY) -m gaia.tools.check_llm

# --- frontend --------------------------------------------------------------

.PHONY: dev-web
dev-web: ## Run the Vite dev server
	cd $(ROOT)/frontend && pnpm dev
