# Gaia

Metadata-driven semantic agent. Gaia introspects a database, reasons about what
its metadata means, scores that reasoning against evidence, deepens the evidence
where the score is weak, and answers business questions in natural language by
generating, validating and executing SQL.

Design and rationale: **[OUTLINE.md](OUTLINE.md)**.
Delivery plan, gates and decisions: **[PHASES.md](PHASES.md)**.
Step-by-step operator guide: **[USER_GUIDE.md](USER_GUIDE.md)**.

> Status (2026-11-17): **Phases 0–2 complete.** Phase 3: 3.P, 3.0, 3A (PostgreSQL) and 3B
> done; the 3C evaluation is running on the owner's host (B-C, B-P, N-C, N-P complete).
> **Phase 4 built and verified on the host**: 4A (the SQL guard, the read-only executor),
> 4B (retrieval), 4C (answering questions), 4D (golden runs), 4E (the chat API), 4F (data
> sources added at runtime) (`make query`, `make retrieve`, `make ask`, `make golden`,
> `make dev-api`, `make datasource`). The configured model's golden run on `demo_pg`:
> **executable 100 %, correct 97 %** (targets 80 / 70); the gate awaits the owner's tick.
> **Phase 5 is re-planned** (sign-in through the company directory, three roles,
> need-to-know, SME review, chat, administration) and awaits approval. PostgreSQL is the main
> dialect (D52); SQL Server is verified asynchronously on delivered builds (D56). What to run
> on your machine: **[OWNER_VERIFICATION.md](OWNER_VERIFICATION.md)** (§21 is the latest
> triage). See [PHASES.md](PHASES.md).

---

## Layout

```text
gaia/
├── config/gaia.yaml        committed configuration (no secrets)
├── docker-compose.yml      metadata PostgreSQL 18 + Redis (+ optional PG target)
├── infra/postgres/init/    metadata store: extensions + schemas (first-run only)
├── infra/sqlserver/        SQL Server read-only login script
├── infra/pg-target/        PostgreSQL dev target: schema, read-only role, seed
├── backend/                FastAPI app, Alembic, tooling
├── gaia-env/               Python 3.12 virtualenv (git-ignored)
├── frontend/               React + TS + Vite (Phase 5)
├── scripts/owner_runs.sh   the owner's asynchronous verification runs, in tracks
└── tests/                  unit and integration tests
```

## Prerequisites

* Docker + Compose
* Python 3.12 (`gaia-env` already created at the repo root)
* For SQL Server targets: ODBC Driver 18 on the host (`msodbcsql18`) and a running
  SQL Server 2025 container (`sql1`) with the target database restored
* For the PostgreSQL development target: nothing extra (`psycopg` is a backend
  dependency; the server runs in Compose)
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

**Phases 0, 1 and 2** are complete and verified end to end against the live
SQL Server target. Phase 3 is approved: 3.P is done, 3.0 is code complete, and
3B (reasoning with evidence) and 3A (evidence collection) are done in the
sandbox on PostgreSQL; the owner's host runs are listed in
[OWNER_VERIFICATION.md](OWNER_VERIFICATION.md).

| Check | Result |
|---|---|
| Metadata DB + Redis | healthy; 6 extensions, 6 schemas |
| Alembic | `0003 (head)` |
| `make check-target` | 7/7 PASS — `nge_small`, 68 tables, 607 columns, read-only proven (since 3.0: 9 checks, read-only proven from permissions too) |
| `make check-embed` | `bge-m3`, 1024 dims |
| `make check-llm` | plain + JSON mode + thinking + usage |
| `make introspect` | 71 objects / 607 columns captured, idempotent on re-run |
| comment-only change | detected via content hash (`modify_date` does not move) |
| `make reason` | 10 tables / 99 columns reasoned, 109 embeddings, 94 join hints |
| re-run `make reason` | `calls=0` — zero LLM calls, 0.3 s |
| score breakdown | 99/99 sum exactly to `raw_score` |
| `pytest` | 357 passed, 8 skipped (Ollama live, SQL Server live) — with the PostgreSQL integration tests enabled (3A, sandbox); 365 expected on the host |

Four non-obvious facts worth remembering:

* Use `127.0.0.1`, never `localhost`, for the SQL Server host — the container
  publishes only on IPv4 and ODBC otherwise stalls with `HYT00`.
* The LLM gateway allows at most **4 concurrent requests per model**, so
  `llm.concurrency` is 3 — one slot is kept free for retries.
* Thinking mode costs roughly 4–5× the latency of a non-thinking call, and a
  single wide-table critique has taken 495 s. `llm.timeout_s` is set to 1800 s
  to cover that; read timeouts retry only once so a slow provider cannot triple
  the worst case.
* The target has **zero** `MS_Description` comments, so meaning must come from
  names, keys and profiled data. That is why no column currently reaches the
  `accepted` band — and under the Phase 2 scoring (`heuristic-v1`) an
  uncommented plain column cannot reach it even once profiled (ceiling 0.775).
  Phase 3B replaces the scoring (PHASES P3-2).

Alembic:

```bash
cd backend && ../gaia-env/bin/alembic upgrade head
```

## Phase 2: semantic reasoning

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

A re-run skips any table whose structure hash, prompt versions, model and —
since Phase 3B — evidence hash are unchanged, so it makes zero LLM calls. A table
whose critique failed is re-run until it has one.

### Phase 3B: reasoning with evidence (the default)

`semantics.prompt_major: 2` and `scoring.version: heuristic-v2` are the defaults.
Each table gets an evidence block next to its bundle; columns are scored from an
evidence ledger (stored in `semantics.evidence_item`), and what the machine
cannot settle goes to the review queue (`semantics.review_item`):

```bash
make reason TARGET=demo_pg ARGS="--table legacy.tb_trx02"        # evidence from the profile store (3A)
make reason TARGET=demo_pg ARGS="--evidence-file ev.json ..."      # or profiles as JSON (gaia.profiling.summary)
make review TARGET=demo_pg                                         # the SME queue, highest priority first
make reason ARGS="--replay /tmp/replay ..."                         # answers from files; model recorded as 'replay'
```

### Phase 3A: evidence from the data

Profiling reads the target (only `SELECT`, read-only session, timeouts, MAXDOP,
page and time budgets, one run per data source) and stores aggregates — never
sample rows — in the `profiling` schema (migration `0005`):

```bash
cd backend && ../gaia-env/bin/alembic upgrade head    # adds 0005_profiling
make profile-dry TARGET=demo_pg          # plan: profile / reuse / skip, no target reads
make profile     TARGET=demo_pg          # Stage 1: profiles, view lineage, join candidates
make reason      TARGET=demo_pg          # Stage 2: reasoning with that evidence
make profile-tests TARGET=demo_pg        # Stage 3: tests of the model's claims
make reason      TARGET=demo_pg          # re-reasons only tables whose evidence changed
```

A second `make profile` inside the freshness window (same structure, row count
within 20 %, younger than 30 days) reads no data. `--report` lists every join
candidate tested, `--force` re-profiles, `--ignore-window` runs outside
`profiling.window`. SQL Server may auto-create statistics while profiling; the
run lists them (footprint). Turn profiling off for a target with
`targets.<name>.profiling_enabled: false` — reasoning then uses catalog evidence
only and routes low scores to review.

Switching from Phase 2 prompts re-reasons every table once (the prompt version
changes). To keep Phase 2 behaviour: `semantics.prompt_major: 1` and
`scoring.version: heuristic-v1`. Other options: `--schema`, `--limit`,
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
schemas. Without it those tests are *skipped* — which is why the single-column
re-reasoning check never ran on the host before 3.0.

```bash
make test-db                                   # creates gaia_test in the metadata container
GAIA_TEST_DATABASE_URL=postgresql+asyncpg://gaia:<password>@127.0.0.1:5433/gaia_test \
GAIA_TEST_PG_TARGET_URL=postgresql://gaia_ro:<pw>@127.0.0.1:5434/gaia_target_pg \
GAIA_TEST_MSSQL=1 \
make test                                      # everything, including live target tests
make test ARGS="tests/integration/test_semantics_pipeline_pg.py -k single_column"
```

`GAIA_TEST_MSSQL=1` runs the live SQL Server checks (statement timeout, probe,
catalog determinism, the profiler on the smallest and the largest table, module
definitions and view lineage) against the configured target with the read-only
login. `GAIA_TEST_PG_TARGET_URL` adds the PostgreSQL target: the dialect, the
whole 3A pipeline (`test_profiling_pg.py`) and the replay fixtures.

## Target database

Gaia never creates or modifies a real target database. For SQL Server the owner
restores the backup (OUTLINE §4.1) and provisions the read-only login with
[`infra/sqlserver/001_readonly_login.sql`](infra/sqlserver/001_readonly_login.sql).

After the restore, set `GAIA_TARGET_DATABASE` in `.env`, then:

```bash
make check-target     # asserts: connect, catalog read, comments, DMV, read-only
make target-info      # same, plus a per-schema object summary
```

`check-target` fails deliberately when the login can write — that is the point.
Every target-facing command accepts `TARGET=<name>` (default:
`app.default_target`), e.g. `make check-target TARGET=demo_pg`.

### PostgreSQL development target (D25)

A second, synthetic target that Gaia generates itself — the one exception to
"Gaia never creates the target", because it is a fixture. It is a separate
server (`127.0.0.1:5434`) from the metadata store and is built for the Phase 3
worst case: almost no comments, a `legacy` schema with meaningless names
(`tb_trx02.f1…f10`) and undeclared references, `NOT VALID` and disabled
constraints, enum/domain types, partitions, generated columns, and views with
pass-through and computed columns. All personal data is synthetic.

```bash
# .env: set GAIA_PG_TARGET_PASSWORD and GAIA_PG_TARGET_ADMIN_PASSWORD/_URL
make pg-target-up                          # postgres:18 on 127.0.0.1:5434 (Compose profile)
make pg-target-seed                        # ~3 M rows, deterministic (ARGS="--scale 0.1" for a quick one)
make check-target   TARGET=demo_pg         # 9 checks; read-only proven from privileges + a DDL attempt
make introspect     TARGET=demo_pg         # 28 objects (24 tables, 4 views), 213 columns
make reason-dry     TARGET=demo_pg ARGS="--table legacy.tb_trx02 --show-bundles"
```

To include it in `make test`:
`GAIA_TEST_PG_TARGET_URL=postgresql://gaia_ro:<pw>@127.0.0.1:5434/gaia_target_pg make test`.

## Services and ports

| Service | Port | Managed by |
|---|---|---|
| Metadata PostgreSQL 18 + pgvector | `127.0.0.1:5433` | `docker compose` |
| Redis 8 | `127.0.0.1:6380` | `docker compose` |
| FastAPI | `127.0.0.1:8000` | `make dev-api` |
| Vite dev server | `127.0.0.1:5173` | `make dev-web` (Phase 5) |
| SQL Server 2025 (`sql1`) | `127.0.0.1:1433` | external (owner) |
| PostgreSQL dev target (`gaia-target-pg`) | `127.0.0.1:5434` | `make pg-target-up` (Compose profile `target-pg`) |
| Ollama (`ollama-code`) | `127.0.0.1:11435` | external (owner) |

## Configuration model

* `config/gaia.yaml` — committed, describes behaviour. Contains `${VAR}`
  placeholders only, or `${VAR:-default}` for non-secret values.
* `.env` — git-ignored, holds secrets. Never committed.

Placeholders are resolved at load time; unresolved names are reported by
`/health` and `/api/meta` instead of failing with an opaque error.
`check-target` only fails on placeholders that matter for the target being
checked, so a missing PostgreSQL password never breaks SQL Server checks.

Key decisions already fixed:

| Concern | Choice |
|---|---|
| Reasoning LLM | `deepseek-flash` via OpenAI-compatible gateway (`GAIA_LLM_MODEL`; `deepseek-v4.1-flash` until 2026-11-16), JSON mode + thinking mode |
| Embeddings | local Ollama `bge-m3`, 1024 dims, CPU (`num_gpu: 0`) |
| Target databases | SQL Server 2025, `nge_small` (68 tables, 3 views, 607 columns, 0 comments); PostgreSQL dev target `demo_pg` (24 tables, 4 views, 213 columns, 3 comments) |
| Design premise | worst case by default: no comments, no dictionary, possibly meaningless names; an SME per database is the ground truth (D23) |
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
| `make profile-dry` / `make profile` | Phase 3A Stage 1: profile the target, view lineage, join candidates (`ARGS=...`, `TARGET=...`) |
| `make profile-tests` | Phase 3A Stage 3: tests of the stored claims (after `make reason`) |
| `make reason-dry` / `make reason` | reasoning over the stored snapshot and evidence (`ARGS=...`, `TARGET=...`) |
| `make measure` | Phase 3.0 profiling-cost measurements: timings, pages, footprint (`TARGET=...`, `ARGS=...`) |
| `make test-db` | create the disposable `gaia_test` database for the integration tests |
| `make review` | the SME review queue, highest priority first (`TARGET=...`) |
| `make query` / `make query-check` | Phase 4A: one SQL statement through the SQL guard, run read-only and recorded / validated only (`TARGET=...`, `ARGS="--sql '…'"` or `--file`) |
| `make dev-api` | the API on 127.0.0.1:8000, with the chat endpoints of Phase 4E (`/api/conversations`, server-sent events) |
| `make golden` / `make clarifications` | Phase 4D: run a golden question set, scored by result / clarifications and Gaia's agreement with users (D61) |
| `make ask` | Phase 4C: ask a business question — a table and a narrative, or a clarifying question (`TARGET=...`, `ARGS="--question '…'"`) |
| `make retrieve` / `make retrieval-eval` | Phase 4B: the schema context for a question / recall over a question set (`TARGET=...`, `ARGS=...`) |
| `make evaluate*` | Phase 3C evaluation (plan, labels, run, judge, audit, report, cost); `make evaluate-freeze` freezes a run's inputs (`<out>/inputs.json`; the first run does it too) |
| `make datasource` / `make master-key` | Phase 4F: data sources added at runtime — add, probe, list, update, disable/enable, remove, rotate-key / a new `GAIA_MASTER_KEY` |
| `scripts/owner_runs.sh setup\|quick\|gateway\|gateway-long` | the owner's asynchronous runs in tracks (OWNER_VERIFICATION "Run plan"); `GAIA_LLM_CONCURRENCY=2` when two gateway tracks share the gateway |
| `make stamp` | which code this is: build, sqlglot version, git commit (first line of every verification output) |
| `make pg-target-up` / `pg-target-seed` / `pg-target-psql` | PostgreSQL development target |
| `make pull-embed` | pull the embedding model |

## Roadmap

Phases 0, 1 and 2 are complete. Phase 2 (semantic reasoning, evidence scoring,
embeddings) is verified against `nge_small` and committed as `3585eba`; its
acceptance checklist and open items are in PHASES.md.

**Phase 3** (approved 2026-11-14) runs in gated sub-phases: **3.P** PostgreSQL
target and dialect (done) → **3.0** preconditions (code complete; SQL Server runs
pending) → **3B** reasoning with evidence and scoring v2 (done in the sandbox,
built before 3A at the owner's request) → **3A** evidence collection (done in the
sandbox) → **3C** evaluation on a name-blind worst-case simulation (harness built,
runs on the owner's host). **Phase 4** (approved 2026-11-16) starts with **4A**, the
SQL guard and executor (done), then retrieval (4B, done), answering (4C, done), the golden
runs (4D, done), the API (4E, done) and data-source management (4F). The 99 columns reasoned so far are 65 `deep_profile`,
32 `deep_profile_flagged` and 2 `abstain`; they are the catalog-only baseline
(condition N-C). SME review moves forward into Phase 5 (roadmap R1).
**Phase 5** (re-planned 2026-11-17, awaiting approval) runs in stages: 5.0 triage fixes →
5A identity (the company directory via LDAP/AD, a bootstrap admin, sessions, roles) → 5B SME
review → 5C need-to-know (data categories granted to directory roles, enforced in retrieval
and the SQL guard) → 5D the chat screen → 5E a read-only runs view; scheduling runs and
switching models from the admin screen are proposed for later (PHASES, Phase 5).
