# Gaia

Metadata-driven semantic agent. Gaia introspects a database, reasons about what
its metadata means, scores that reasoning against evidence, deepens the evidence
where the score is weak, and answers business questions in natural language by
generating, validating and executing SQL.

Design and rationale: **[OUTLINE.md](OUTLINE.md)**.
Delivery plan, gates and decisions: **[PHASES.md](PHASES.md)**.

> Status: **Phase 0 and Phase 1 complete.** Phase 2 (semantic reasoning and
> evidence scoring) is **implemented and awaiting live verification**; see
> [PHASES.md](PHASES.md).

---

## Layout

```text
gaia/
├── config/gaia.yaml        committed configuration (no secrets)
├── docker-compose.yml      metadata PostgreSQL 18 + Redis only
├── infra/postgres/init/    extensions + schemas (first-run only)
├── infra/sqlserver/        restore runbook + read-only login script
├── backend/                FastAPI app, Alembic, tooling
├── gaia-env/               Python 3.12 virtualenv (git-ignored)
├── frontend/               React + TS + Vite (Phase 5)
└── tests/                  unit and integration tests
```

## Prerequisites

* Docker + Compose
* Python 3.12 (`gaia-env` already created at the repo root)
* ODBC Driver 18 for SQL Server on the host (`msodbcsql18`)
* A running SQL Server 2025 container (`sql1`) with the target database restored
* Ollama container (`ollama-code`) with `bge-m3` pulled

## Quick start

```bash
cp .env.example .env          # then fill in real values
make venv                     # install backend deps into gaia-env
make up                       # start metadata DB + Redis
make verify-extensions        # vector, pg_trgm, unaccent, btree_gin, pgcrypto, pg_stat_statements
make check-env                # import + ODBC driver sanity
make check-embed              # Ollama embedding smoke test
make check-llm                # reasoning LLM: plain, JSON mode, thinking
make dev-api                  # http://127.0.0.1:8000/health
```

## Status

**Phase 0** and **Phase 1** are complete and verified end to end.

| Check | Result |
|---|---|
| Metadata DB + Redis | healthy; 6 extensions, 6 schemas |
| Alembic | `0002 (head)` |
| `make check-target` | 7/7 PASS — `nge_small`, 68 tables, 607 columns, read-only proven |
| `make check-embed` | `bge-m3`, 1024 dims |
| `make check-llm` | plain + JSON mode + thinking + usage |
| `make introspect` | 71 objects / 607 columns captured, idempotent on re-run |
| comment-only change | detected via content hash (`modify_date` does not move) |
| `pytest` | 51 passed |

Three non-obvious facts worth remembering:

* Use `127.0.0.1`, never `localhost`, for the SQL Server host — the container
  publishes only on IPv4 and ODBC otherwise stalls with `HYT00`.
* Thinking mode costs roughly 4–5× the latency of a non-thinking call. Keep it
  for semantics and critique only.
* The target has **zero** `MS_Description` comments, so meaning must come from
  names, keys and profiled data.

Alembic:

```bash
cd backend && ../gaia-env/bin/alembic upgrade head
```

## Phase 2: semantic reasoning (awaiting live verification)

Reasoning runs over the **stored** snapshot, so introspect first:

```bash
cd backend && ../gaia-env/bin/alembic upgrade head    # adds 0003_semantics
make introspect
make reason-dry ARGS="--table dbo.transactions --with-neighbours"   # no LLM calls, no writes
make reason     ARGS="--table dbo.transactions --with-neighbours"   # PHASES decision 1 (c)
make reason                                                          # then everything
make reason     ARGS="--cost-report"                                 # per-call tokens/latency
```

Per table: one reasoning call and one critique call to DeepSeek (thinking per
`llm.thinking.semantics` / `.critique`), Pydantic validation with one
corrective retry, scoring, a new version in `semantics.table_semantic` /
`column_semantic`, inferred joins in `join_hint`, and every call (including
failures and raw payloads) in `audit.llm_call`. New semantics are then
embedded with Ollama `bge-m3` into `semantics.embedding`.

A re-run skips any table whose structure hash, prompt versions and model are
unchanged, so it makes zero LLM calls. Other options: `--schema`, `--limit`,
`--no-thinking`, `--force`, `--no-embed`, `--no-cache`, `--show-bundles`
(with `--dry-run`, prints the exact prompt).

### Embeddings client

`gaia.llm.embeddings` wraps Ollama's `/api/embed` using the `embeddings`
section of `config/gaia.yaml` (resolved from `GAIA_EMBEDDINGS_*` in `.env`).

```python
from gaia.core.config import get_settings
from gaia.llm.cache import build_cache
from gaia.llm.embeddings import build_embedding_client

s = get_settings()
with build_embedding_client(s.embeddings, cache=build_cache(s.cache.redis_url)) as client:
    result = client.embed(["customer order total", "invoice due date"])
    result.vectors        # list[list[float]], input order, len == embeddings.dimensions
```

Inputs are batched by `batch_size` and sent with `options.num_gpu`. Vectors
are cached in Redis as `embed:{model}:{sha256(text)}`. Every vector is checked
against `dimensions` and for NaN/Inf. A missing model raises an error naming
the `ollama pull` command. `make check-embed` runs through this client,
without the cache.

### Tests against a real PostgreSQL

`tests/integration/test_semantics_pipeline_pg.py` runs the full pipeline
(with a fake LLM and a fake embedder) against a **disposable** database. It
runs only when `GAIA_TEST_DATABASE_URL` is set and the database name contains
`test`, because it drops and recreates the `catalog`, `semantics` and `audit`
schemas:

```bash
docker exec gaia-metadata-db createdb -U gaia gaia_test
GAIA_TEST_DATABASE_URL=postgresql+asyncpg://gaia:<password>@127.0.0.1:5433/gaia_test make test
```

## Target database

Gaia never creates or modifies the target database. The owner restores it and
provisions a read-only login; see **[infra/sqlserver/RESTORE.md](infra/sqlserver/RESTORE.md)**.

After the restore, set `GAIA_TARGET_DATABASE` in `.env`, then:

```bash
make check-target     # asserts: connect, catalog read, comments, DMV, read-only
make target-info      # same, plus a per-schema object summary
```

`check-target` fails deliberately when the login can write — that is the point.

## Services and ports

| Service | Port | Managed by |
|---|---|---|
| Metadata PostgreSQL 18 + pgvector | `127.0.0.1:5433` | `docker compose` |
| Redis 8 | `127.0.0.1:6380` | `docker compose` |
| FastAPI | `127.0.0.1:8000` | `make dev-api` |
| Vite dev server | `127.0.0.1:5173` | `make dev-web` (Phase 5) |
| SQL Server 2025 (`sql1`) | `127.0.0.1:1433` | external (owner) |
| Ollama (`ollama-code`) | `127.0.0.1:11435` | external (owner) |

## Configuration model

* `config/gaia.yaml` — committed, describes behaviour. Contains `${VAR}`
  placeholders only.
* `.env` — git-ignored, holds secrets. Never committed.

Placeholders are resolved at load time; unresolved names are reported by
`/health` and `/api/meta` instead of failing with an opaque error.

Key decisions already fixed:

| Concern | Choice |
|---|---|
| Reasoning LLM | `deepseek-v4.1-flash` via OpenAI-compatible gateway, JSON mode + thinking mode |
| Embeddings | local Ollama `bge-m3`, 1024 dims, CPU (`num_gpu: 0`) |
| Target database | SQL Server 2025, `nge_small` (68 tables, 3 views, 607 columns, 0 comments) |
| Metadata store | PostgreSQL 18 + pgvector, `vector(1024)` |
| Cache | Redis, ephemeral only |

## Useful commands

| Command | Purpose |
|---|---|
| `make help` | list all targets |
| `make up` / `make down` | start / stop the stack |
| `make reset-db` | destroy and re-initialise the metadata DB |
| `make psql` / `make redis-cli` | shells into the containers |
| `make test` | run the test suite |
| `make reason-dry` / `make reason` | Phase 2 reasoning over the stored snapshot (`ARGS=...`) |
| `make pull-embed` | pull the embedding model |

## Roadmap

Phases 0 and 1 are done. Phase 2 (semantic reasoning, evidence scoring,
embeddings) is implemented and awaiting live verification against `nge_small`;
its acceptance checklist is in PHASES.md. Next: **Phase 3**, deep profiling and
re-reasoning for columns below the thresholds. See OUTLINE.md §17.
