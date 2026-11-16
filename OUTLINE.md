# Gaia — Outline (revised)

> Metadata-driven semantic agent: introspect a database, reason about what its
> metadata *means*, score that reasoning honestly, deepen the evidence where the
> score is weak, and answer business questions in natural language by generating,
> validating and executing SQL.
>
> Status (2026-11-16): Phases 0–2 done; Phase 3: 3.P, 3.0, 3A (PostgreSQL), 3B
> done, 3C runs resuming on the host; **Phase 4 built in the sandbox — 4A (SQL guard,
> executor), 4B (retrieval), 4C (answering), 4D (golden runs), 4E (chat API) and 4F
> (data sources added at runtime)**; the gate waits for the configured model's golden
> run on the host. Delivery state, gates and
> decisions live in [PHASES.md](PHASES.md), which takes precedence over this file
> where they differ (D12′: config > decisions log > PHASES > OUTLINE).
> Metadata store: PostgreSQL 18 + pgvector. Targets: **PostgreSQL (main dialect
> since D52, 2026-11-16)** and SQL Server 2025 (the first target; since D52 its
> live results are asynchronous and non-blocking). Reasoning LLM: configurable per deployment;
> development uses `deepseek-v4.1-flash` via a gateway. Embeddings: local Ollama
> `bge-m3`. Frontend: React + TypeScript (Vite, pnpm).
>
> Design premise (D23): assume the worst case — no comments, no data dictionary,
> possibly meaningless names, many databases per deployment — with a
> human-in-the-loop SME per database as the ground truth.

---

## 1. Product summary

Two connected pipelines.

### A. Background semantic modelling

1. Connect to a target database with a **read-only** account.
2. Extract catalog metadata into a versioned canonical JSON snapshot:
   schemas, tables, views, columns, data types, nullability, defaults,
   computed/identity flags, PK/unique/check constraints, foreign keys, indexes,
   table and column comments, partitions, row estimates, sizes.
3. Collect **cheap evidence first**: module and view definitions, view lineage,
   statistics histograms, and a bounded profile of the data (size-aware
   sampling, semantic-type detectors, cross-column identities, join candidates).
   A catalog-only path remains for targets where data may not be read.
4. Reason **once** per table bundle with all of that evidence, producing a
   structured semantic model per table and column: business name, purpose, role,
   entity, unit, value domain, synonyms, join hints, example business questions,
   evidence, alternatives.
5. Compute an **evidence-grounded confidence score** from a ledger of supporting
   and contradicting evidence — not from the model's self-report, and never from
   names alone.
6. **Verify** the claims that remain uncertain with targeted tests against the
   target (inclusion, uniqueness, exact distributions), within a budget.
7. Re-reason only the tables whose evidence changed.
8. Persist everything in Gaia's own PostgreSQL store, with JSONB and pgvector.
9. Hand what remains to the **SME of that database** as a ranked review queue;
   their decisions are ground truth and the calibration source.

### B. Online chat-to-SQL

1. User asks a business question in the chat.
2. Retrieve relevant tables, columns, joins, glossary terms and descriptions.
3. Build a compact schema context and a query plan.
4. Generate SQL in the target dialect.
5. Validate it structurally and against the catalog.
6. Execute read-only, with timeouts and row limits.
7. Repair on failure, bounded to a small number of attempts.
8. Return the natural-language answer, the SQL, the result set, assumptions,
   confidence, and a clarification request when the question is ambiguous.

**Hard rule:** Gaia's metadata store and the target database are separate.
Gaia never treats its own store as a business target.

---

## 2. Verified environment

| Item | Value |
|---|---|
| Python | 3.12.3 |
| Node / npm / pnpm | 24.13.1 / 11.8.0 / 0.34.6 (corepack) |
| Docker / Compose | 29.9.3 / v5.7.1 |
| CPU / RAM / Disk free | 8 cores / 31 GB / 281 GB |
| GPU | GTX 1050 Ti 4 GB — embeddings on CPU (`num_gpu: 0`) |
| Python env | `gaia-env` (pre-created by owner, Python 3.12.3) |
| Host ODBC | `msodbcsql18 18.4.1.1`, `unixodbc-dev`, gcc 13.3 → `pyodbc` builds natively |
| Target SQL Server | container `sql1`, external to Compose, `127.0.0.1:1433` (never `localhost`, D11) |
| SQL Server version | 2025 RTM-CU7, 17.0.4065.4 |
| Ollama | container `ollama-code`, `127.0.0.1:11435`, v0.17.7, `/api/embed` confirmed |
| PostgreSQL dev target | Compose service `gaia-target-pg` (profile `target-pg`), `127.0.0.1:5434`, synthetic (D25) |

Reserved host ports: `5433` (metadata pg18), `6380` (redis), `8000` (API),
`5173` (Vite), `5434` (PostgreSQL dev target). External: `1433` (SQL Server),
`11435` (Ollama).

### SQL Server 2025 as non-root

SQL Server 2025 runs as uid `10001 (mssql)`. A bind mount under a `0700`
parent directory fails with:

```
Error: The system directory [/.system] could not be created.
[Status: 0xC0000022 Access denied errno = 0xD(13)]
```

Also required: `--shm-size 2g` (default 64 MB is too small), a well-formed
`-p 127.0.0.1:1433:1433`, and `--restart unless-stopped`. Note that `udisks2`
remounts removable media as `1000:1000 0700` on every boot, so an external-disk
bind mount needs an `/etc/fstab` entry or it will break again after reboot.
A named Docker volume avoids the problem entirely.

---
## 3. Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Reasoning LLM | Configurable per deployment (local or remote, A7); development uses `deepseek-v4.1-flash` via an OpenAI-compatible gateway (D1) | Customers choose the model; the pipeline must not depend on one provider's features (thinking mode, huge contexts) |
| Thinking mode | On for semantics + critique, off for SQL repair and classification | Thinking is the default and must be explicitly disabled per task to control latency |
| Structured output | `response_format={"type":"json_object"}` + Pydantic validation + bounded retry | JSON mode guarantees valid JSON, not schema conformance; blank content must be retried |
| Prompt caching | Byte-identical stable prefix, volatile question last | Cache-hit input ≈ 1/50th of cache-miss price — the largest cost lever |
| Off-peak scheduling | Bulk re-reasoning prefers off-peak | Peak = 01:00–04:00 and 06:00–10:00 UTC weekdays; off-peak is half price |
| Embeddings | **Local Ollama `bge-m3`, 1024-dim, `num_gpu: 0`** | No cloud embedding cost; schema text stays on-host; multilingual for future i18n |
| Never embed with a coder model | `qwen2.5-coder` returns 1536 dims of untrained noise | Would silently destroy retrieval quality |
| Target DB | SQL Server 2025 first; **PostgreSQL** dialect added in 3.P (D25, D26); **PostgreSQL is the main dialect since D52** (2026-11-16): dialect work lands and is accepted there first, SQL Server follows with tests whose live results do not block | Both behind the dialect seam, each with its own type vocabulary. D52: the owner-only SQL Server round trip had become the critical path |
| Target provisioning | **Owner restores a backup; Gaia never creates the database** (the synthetic PostgreSQL dev target is the one exception, D25) | The expectation that the vendor DB carried comments proved false — it has none — which led to the worst-case premise (D23) |
| Read-only login | Created manually by owner; **Gaia verifies, never creates** | Target database stays untouched by the app |
| SQL dialect | `sqlglot` with `read`/`write` = `tsql` or `postgres` | Required for AST validation of generated SQL and for view lineage |
| MSSQL driver | `pyodbc` + host ODBC Driver 18 | Already installed on the host; no container needed |
| PostgreSQL driver | `psycopg` 3 | Already a backend dependency; sessions pinned read-only with timeouts and `search_path = pg_catalog` |
| Backend runtime | **Host process in `gaia-env`** (not containerised in Phase 0) | Uses the owner's pre-created venv; simplest iteration |
| Metadata store | PostgreSQL 18 + pgvector on **5433** | Not reusing the stopped pg15 `vectordb` container |
| Cache | Redis 8 on **6380**, ephemeral only | Postgres remains the source of truth |
| PII | Detected as part of `semantic_type` on the profile (3A); values reach a **remote** model only masked, a local model raw (D24); FPE later | Answer #3, revised 2026-11-14 |
| SME review | Schema + `status` states from day one; ranked review queue in Phase 3; review UI first in Phase 5 (R1) | Answer #8, revised 2026-11-14: human-in-the-loop is the core of the product |
| i18n | i18next in frontend from the start (English only); `locale` on conversations and semantic rows | Answer #6 |
| Frontend tooling | **pnpm** + Vite + React + TS | Answer #5, owner had no preference |
| Data types | PostGIS / geospatial / exotic types out of MVP scope | Answer #10 |
| Scale | No scale target; aim at warehouses and production replicas | Answer #7 |

---

## 4. SQL Server 2025 as first target

### 4.1 Restore runbook (owner-executed)

`RESTORE DATABASE` creates the database itself, so nothing is pre-created.

```bash
docker exec -it sql1 /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa \
  -P "$MSSQL_SA_PASSWORD" -C \
  -Q "RESTORE FILELISTONLY FROM DISK='/backup/nge-small-mssql-v0.3.0.bak'"
```

Using the returned logical names:

```sql
RESTORE DATABASE nge_small
FROM DISK = '/backup/nge-small-mssql-v0.3.0.bak'
WITH MOVE 'nge_small'     TO '/var/opt/mssql/data/nge_small.mdf',
     MOVE 'nge_small_log' TO '/var/opt/mssql/data/nge_small_log.ldf',
     RECOVERY, STATS = 5;
```

Requires roughly 7–15 GB free depending on data/log sizing, plus restore
scratch space.

### 4.2 Read-only login (owner-executed, once per restored DB)

```sql
USE nge_small;
CREATE LOGIN gaia_ro WITH PASSWORD = N'<strong-password>', CHECK_POLICY = OFF;
CREATE USER gaia_ro FOR LOGIN gaia_ro;
ALTER ROLE db_datareader ADD MEMBER gaia_ro;      -- SELECT only
GRANT VIEW DEFINITION TO gaia_ro;                 -- metadata visibility
GRANT VIEW DATABASE STATE TO gaia_ro;             -- DMV row counts / usage stats
```

Gaia **verifies** this login (`make check-target`: can read, cannot write) and
never creates or alters it.

### 4.3 Catalog extraction (read-only)

| What | Source |
|---|---|
| Schemas / tables / views | `sys.schemas`, `sys.objects`, `sys.tables`, `sys.views` |
| Columns & types | `sys.columns`, `sys.types`, `sys.computed_columns`, `sys.identity_columns` |
| **Comments** | `sys.extended_properties` where `name = 'MS_Description'` (table *and* column) |
| Primary / unique keys | `sys.key_constraints`, `sys.indexes`, `sys.index_columns` |
| Foreign keys | `sys.foreign_keys`, `sys.foreign_key_columns` (+ `is_disabled`, `is_not_trusted`) |
| Check constraints | `sys.check_constraints` (definition, `is_not_trusted`) |
| Defaults | `sys.default_constraints` |
| Indexes | `sys.indexes`, `sys.index_columns`, `sys.dm_db_index_usage_stats` |
| Row counts / size | `sys.dm_db_partition_stats`, `sys.partitions` |
| Statistics freshness | `sys.stats`, `sys.dm_db_stats_properties` (row_count, stats_date, modification_counter) |
| Partitions / collation / compression | `sys.partition_functions`, `sys.partition_schemes`, `sys.partitions.data_compression` |

### 4.4 Nuances encoded as first-class concerns

- **`sys.objects.modify_date` is not a reliable change signal.**
  `sp_addextendedproperty` (adding a comment) does **not** bump it. Incremental
  discovery must trust a **content hash**; `modify_date` is a cheap pre-filter only.
- **`DBCC SHOW_STATISTICS`** requires elevated permissions in some configs →
  opt-in capability flag, off by default.
- **`sys.dm_db_partition_stats`** needs `VIEW DATABASE STATE`; without it, row
  counts fall back to `sys.partitions.rows` (approximate).
- Session policy enforced by Gaia as well as the login:
  `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`, `SET LOCK_TIMEOUT`,
  driver-level `timeout`, `rowcount` caps. *The statement (driver) timeout was
  configured but not applied on SQL Server sessions until Phase 3.0; it is now
  set on every session (`dialects/mssql/connection.py`), and a timeout surfaces
  as `StatementTimeout`. PostgreSQL sessions apply `statement_timeout`,
  `lock_timeout`, a read-only transaction and `search_path = pg_catalog` (3.P).*

---

## 5. Architecture

```text
┌─────────────────────┐
│  Target SQL Server  │  read-only login (gaia_ro), external container
│  2025 (sql1:1433)   │
└─────────┬───────────┘
          │ catalog + stats + samples
          ▼
┌──────────────────────────────┐
│ Introspection service        │
│ - dialect catalog reader     │
│ - canonical JSON snapshot    │
│ - structure/data hashes      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐       ┌──────────────────────┐
│ Semantic reasoning pipeline  │◄──────│ Business glossary /  │
│ - table reasoning            │       │ SME corrections      │
│ - column reasoning           │       └──────────────────────┘
│ - critique / alternatives    │
│ - confidence scoring         │
└──────────────┬───────────────┘
               │ score < threshold?
               ▼
┌──────────────────────────────┐
│ Deep profiling service       │
│ - nulls, distinct, min/max   │
│ - samples, top-k, patterns   │
│ - FK cardinality, orphans    │
│ - PII classification         │
│ - budget + timeout control   │
└──────────────┬───────────────┘
               ▼
┌─────────────────────────────────────────────┐
│ PostgreSQL 18 metadata store (:5433)        │
│ JSONB snapshots + versioned semantics       │
│ pgvector embeddings + trigram search        │
└──────────────────┬──────────────────────────┘
                   │ retrieval
                   ▼
┌──────────────────────────────┐
│ Chat / SQL agent             │
│ retrieve → plan → generate   │
│ → validate → execute → answer│
└──────────────┬───────────────┘
               │
      ┌────────┴────────┐
      ▼                 ▼
 React UI (:5173)   Redis cache (:6380)
```

External dependencies: `sql1` (SQL Server 2025), `ollama-code` (embeddings,
optionally a local LLM), DeepSeek API (default reasoning LLM).

### Components

| Component | Responsibility |
|---|---|
| FastAPI API | Chat, target management, admin/review endpoints, SSE streaming |
| Worker (`arq`) | Profiling jobs, reasoning batches, snapshot refresh, embeddings |
| Introspector | Dialect-specific catalog extraction + JSON normalization |
| Profiler | Cheap statistics first, targeted deep profiling second |
| Semantic reasoner | Table- and column-level reasoning with structured outputs |
| Confidence service | Computes and calibrates scores from evidence |
| Retrieval service | Hybrid semantic + keyword + relationship search |
| SQL agent | Planning, T-SQL generation, validation, execution, repair |
| PostgreSQL | Durable system of record |
| Redis | Ephemeral cache, locks, rate limits, job queue |
| Evaluation harness | Golden questions, calibration metrics, regression tests |

### Stack

- **FastAPI** + Uvicorn
- **SQLAlchemy 2 async** + `asyncpg` for the metadata DB
- `pyodbc` for SQL Server targets (host ODBC Driver 18), `psycopg` 3 for PostgreSQL
  targets; dialect drivers load lazily
- **Alembic** migrations
- **Pydantic v2** + `pydantic-settings`
- **httpx** DeepSeek client (OpenAI-compatible) + Ollama client, one interface
- **sqlglot** for SQL parsing (T-SQL, PostgreSQL), AST validation, view lineage
- **Redis** + **arq** for jobs and caching
- **pgvector** + Ollama `bge-m3` embeddings
- **structlog** structured logging
- **pytest**, `pytest-asyncio`
- Frontend: **Vite + React + TypeScript + i18next + pnpm**

Deliberately **no heavy agent framework**: an explicit, testable pipeline state
machine is easier to debug, evaluate and calibrate than nested agent
abstractions.

---

## 6. Container topology

Compose owns Gaia's own infrastructure plus, behind the `target-pg` profile, the
synthetic PostgreSQL development target. SQL Server and Ollama are external.

| Service | Image | Host port | Notes |
|---|---|---|---|
| `gaia-metadata-db` | `pgvector/pgvector:pg18` | `127.0.0.1:5433` | `PGDATA=/var/lib/postgresql/18/docker`; volume mounts `/var/lib/postgresql` |
| `gaia-redis` | `redis:8-alpine` | `127.0.0.1:6380` | `appendonly no`, `maxmemory-policy allkeys-lru` |
| `gaia-target-pg` | `postgres:18` | `127.0.0.1:5434` | profile `target-pg`; a **target**, never the metadata store (D25) |

| External | Address | Owned by |
|---|---|---|
| SQL Server 2025 (`sql1`) | `127.0.0.1:1433` | owner |
| Ollama (`ollama-code`) | `127.0.0.1:11435` | owner |

Notes:

- Extensions: `vector`, `pg_trgm`, `unaccent`, `btree_gin`, `pgcrypto`,
  `pg_stat_statements`.
- `pg_stat_statements` requires `shared_preload_libraries`.
- Init SQL runs **only** on first initialization of an empty volume →
  document `make reset-db`.
- `pg_isready` healthcheck; timezone UTC.
- Secrets in `.env`; `.env.example` committed; real `.env` never committed.
- Backend and frontend run on the host (`gaia-env`, `pnpm`), not in Compose.
- PostGIS is available (`postgis/postgis:18-3.6`) but deliberately omitted.

---

## 7. Configuration

Two layers: committable YAML describing *what*, `.env` holding *secrets*.
**`config/gaia.yaml` is authoritative** (D12′); the sample that used to be
reproduced here drifted (host, concurrency, timeouts) and was removed.

Sections: `app` (default target, locale), `targets.<name>` (dialect, connection,
`options`, `limits`, `capabilities`, `profiling_enabled`, `profiling_window`),
`llm` (provider, model, concurrency, timeouts, thinking per task, retry budgets,
locality, context budget), `embeddings`, `cache` (TTLs), `profiling` (enabled,
window, freshness, seed, `budget`, exposure override, bands), `scoring` (version,
weights, evidence strengths, critique values, calibrator), `semantics`, `query`.

Rules:

- `${VAR}` is resolved from the environment and `.env` at load time;
  `${VAR:-default}` supplies a default for **non-secret** values (D27).
- Unresolved names are reported by `/health` and `/api/meta`; `check-target`
  counts only those of the target being checked, so configuring a second target
  never breaks checks on the first.
- Secrets are never written to YAML or to the database.
- `targets.<name>.dialect` resolves through the registry (`mssql`, `postgres`,
  alias `postgresql`). Adding an engine is a new `gaia.dialects.<name>` package
  (reader, probe, type vocabulary, profiler; guard from Phase 4) plus a
  config block — no schema change.
- Target connections move from config into the database in Phase 4 (§24);
  config then becomes the bootstrap seed.

---

## 8. Metadata store design

Schemas: `catalog`, `profiling`, `semantics`, `chat`, `evaluation`, `audit`.

### `catalog`

- `data_source` — target connection metadata, encrypted secret reference, read-only role
- `snapshot` — versioned JSONB snapshot, hashes, capture time
- `table` — normalized table/view metadata (incl. `is_tombstoned`)
- `column` — normalized column metadata (incl. `is_tombstoned`)
- `constraint` — PK, unique, check, default
- `relation` — FK relationships, composite column ordering, `is_disabled`, `is_not_trusted`
- `index` — index definitions and usage statistics

### `profiling`

Built in Phase 3A (migration `0005`, R4). Profiles are *current or history*
(`is_current`), like semantics.

- `profile_run` — stage 1 or 3, counts, data queries, pages, footprint, notes
- `table_profile` — status (`profiled` / `partial` / `skipped` + reason / `failed`),
  size class, row count, catalog rows at profile time (freshness), pages, runtime,
  queries, isolation, seed; identities in `evidence`
- `column_profile` — the `ColumnProfileSummary` (the 3B contract) plus per-metric
  provenance: `method`, `rows_scanned`, `pages`, `runtime_ms`, `isolation`, `seed`,
  `source` when a scan or sample was shared. Sample rows are never persisted
- `verification_test` — Stage 1 join candidates and Stage 3 tests of claims
  (`origin`, `specific`, row-weighted `metric`)
- `module_definition`, `view_column_lineage` — view/procedure/function code and
  per-view-column lineage (`pass_through` / `computed` / `unresolved`, `converts`)

### `semantics`

- `table_semantic` — versioned table meaning
- `column_semantic` — versioned column meaning
- `join_hint` — inferred or SME-confirmed joins
- `glossary_term` — business terminology and synonyms
- `embedding` — vectors + model name + dimensions

### `chat`

- `conversation` — includes `locale`
- `message`
- `query_run` — question, retrieved context, generated SQL, validation result,
  execution result, latency, tokens, confidence, pinned `snapshot_id` and
  `semantic_version`

### `evaluation`

- `golden_question`, `evaluation_run`, `evaluation_result`

### `audit`

- `llm_call` — prompt hash, model, tokens, cost, latency, raw response, cache hit
- `schema_change` — detected DDL changes

### Design choices

- Store **raw JSONB *and* normalized rows**. JSONB preserves facts we do not yet
  model; normalized rows make querying and joins practical.
- **Version** semantic results instead of overwriting. Query runs pin the
  snapshot and semantic version so answers stay reproducible.
- PostgreSQL 18 native `uuidv7()` for IDs.
- `vector(1024)` with HNSW indexing (matches `bge-m3`); store the embedding
  model name and dimensions so a model swap is an explicit re-embed.
- Generated `tsvector` column + GIN index for full-text search.
- `pg_trgm` GIN indexes for fuzzy name matching.

### `semantics.column_semantic` key fields

`business_name`, `purpose`, `role`, `entity`, `unit`, `value_domain`,
`synonyms text[]`, `example_questions jsonb`, `pii_types text[]`,
`pii_confidence`, `pii_evidence jsonb`, `status`
(`draft | auto_approved | sme_reviewed | sme_rejected | stale`), `locale`,
`evidence_refs jsonb`, `alternatives jsonb`, `unknowns text[]`,
`prompt_version`, `model`, `raw_score`, `calibrated_score`,
`score_breakdown jsonb`, `structure_hash`.

PII fields exist on `column_semantic` from Phase 2 but stay empty: Phase 3
records PII as a family of `semantic_type` on the **profile**, whose lifecycle
follows the data rather than the reasoning version. Masking applies only to
values sent to a remote model (D24).

---

## 9. Snapshot format

```json
{
  "snapshot_id": "...",
  "data_source": "demo_mssql",
  "dialect": "mssql",
  "captured_at": "...",
  "structure_hash": "...",
  "data_hash": "...",
  "tables": [
    {
      "schema": "dbo",
      "name": "orders",
      "kind": "table",
      "comment": "Customer orders",
      "estimated_rows": 1250000,
      "size_bytes": 98765432,
      "columns": [
        {
          "name": "order_total",
          "ordinal": 7,
          "data_type": "decimal(12,2)",
          "nullable": false,
          "default": null,
          "comment": "Gross order amount",
          "is_identity": false,
          "is_computed": false
        }
      ],
      "constraints": [],
      "relations": [],
      "indexes": [],
      "partitions": []
    }
  ]
}
```

---

## 10. Incremental discovery (hashing)

Three independent hashes, because structure and data change on different clocks:

| Hash | Computed from | Invalidates |
|---|---|---|
| `structure_hash` | canonical JSON of columns, types, nullability, defaults, computed/identity flags, comments, PK/unique/check/FK definitions, index definitions | introspection diff + all semantics for that object |
| `data_hash` | `row_count`, `modification_counter` from `sys.dm_db_stats_properties`, `stats_date` (PostgreSQL: `reltuples`, `n_mod_since_analyze`, last analyze) | informational since P3-6 — see the freshness policy below |
| `profile_hash` | profile payload + profiler version + sampling parameters | nothing structural; cache identity |

Rules:

- Content hash is authoritative; `modify_date` is a hint only (comments do not
  bump it).
- A full catalog read is cheap (one pass over `sys.*`). The expensive stages are
  LLM reasoning and deep profiling — so **always re-read the catalog, then diff
  hashes**, rather than trying to skip the catalog read.
- Unchanged tables skip reasoning and reuse the pinned semantic version.
- Removed objects are **tombstoned**, never hard-deleted, so past query runs stay
  reproducible.
- **Column-level** hashes allow surgical re-reasoning when one column changes in
  a 200-column table.
- **Profile freshness policy (P3-6).** A profile is reused unless the structure
  changed, the row count moved beyond `row_count_delta_ratio` (20 %), or it is
  older than `profile_max_age_days` (30). `data_hash` does not invalidate
  profiles: on SQL Server it includes the latest statistics date, which Gaia's
  own profiling queries move (auto-created statistics) and nightly maintenance
  moves too.

---

## 11. Evidence collection and profiling

Approved design (Phase 3); PHASES.md holds deliverables, acceptance criteria and
defaults. Built for both dialects.

### Staged escalation

```text
Stage 0  metadata evidence      no data reads, no LLM      catalog, module/view definitions,
                                                           view lineage, statistics histograms,
                                                           index usage (importance only)
Stage 1  light profile          bounded page reads         tables without catalog-sufficient evidence
Stage 2  reasoning + critique   one LLM pass               all evidence; views after their bases
Stage 3  targeted verification  tests derived from claims  inclusion, uniqueness, exact top-k, ranges;
                                                           re-reason only tables whose evidence changed
Stage 4  review queue           ranked for the SME         whatever is still below accept_at
```

Profiling comes **before** the first reasoning because, in the worst case, a
names-only LLM pass yields almost nothing and costs more than a bounded profile.
A deterministic catalog-sufficiency check skips Stage 1 for well-documented
tables. Without `SELECT` (or with profiling disabled) the pipeline runs
catalog-only and routes to review.

### What a light profile measures

Per table by size class (small → one exact batched scan and exact `GROUP BY`
top-k; columnstore → batched aggregates; large rowstore → row-sized page sample
plus histograms). Per column: null fraction, distinct count/ratio, min/max (`bit`
cast to `int`), text lengths, numeric mean/stddev on a recorded `float` cast,
temporal granularity, parse rates (numeric-/date-like text, `yyyymmdd`
integers), top-k, empty/constant flag. On the sample, in memory: semantic-type
detectors with validators (email, URL, phone, IBAN mod-97, card Luhn, national
ID checksum, GUID, IP, ISO currency/country, dates in text/int, JSON/XML,
boolean-like text), cross-column identities (`a×b=c`, `a+b=c`, temporal order)
and join candidates. Every metric records `method`, `rows_scanned`, `pages`,
`runtime_ms`, `isolation` and `seed`.

### Sampling and safety

- **`TOP (N)` is never a sample.** Tables clustered on an arrival-order key return
  their earliest rows. `TOP (k) … GROUP BY … ORDER BY count` (top-k) is fine.
- SQL Server has only `TABLESAMPLE [SYSTEM]` (pages); PostgreSQL also has
  `BERNOULLI` (row-level, full read). Page samples are clustered, so samples are
  sized in rows across many pages (default 10,000), never 200 rows. Neither
  engine samples plain views.
- Exact where cheap (small tables, columnstore, targeted tests); sampled or
  approximate on large rowstore, always labelled. `APPROX_COUNT_DISTINCT`'s
  error is irrelevant to meaning; cost decides.
- Budgets in pages per table and run, a 180 s per-table ceiling, statement and
  lock timeouts, MAXDOP (`max_parallel_workers_per_gather` on PostgreSQL), one
  profiling session per target, PostgreSQL advisory locks, per-target run
  windows after statistics maintenance.
- **Footprint:** SQL Server may auto-create statistics for profiled columns;
  Gaia reports them (D4′). PostgreSQL has no such side effect.
- Sample rows are processed in memory and never persisted.

### Views and computed columns

View definitions are parsed with `sqlglot` and each view column classified:
**pass-through** (inherits the base column's meaning; its alias becomes evidence
*for the base column*), **computed** (reasoned from its expression and the
resolved inputs), or **unresolved**. Views are reasoned after their base tables.

### As built in 3A (see §28)

- Size classes as designed; *small* tables are exact throughout (one batched
  scan, whole-table values up to 10,000 rows, one `GROUP BY` per column beyond
  that). Large SQL Server tables get one batched `APPROX_COUNT_DISTINCT` scan;
  PostgreSQL uses the planner's estimate for free.
- Joins are discovered from values: shapes for text, ranges and **key density**
  for integers (a dense key matches any in-range integer, so such pairs are not
  evidence). Inclusion is **row-weighted**, so legacy orphans do not hide a
  reference.
- Views that only *convert* a column (`TO_DATE(f3::text, 'YYYYMMDD') AS
  transaction_date`) give the base column an alias too.
- Module definitions live in the profile store, not in the snapshot (D32).
- Not built: statistics histograms, index usage, mean/stddev, fan-out tests.

---

## 12. Honest scoring

An LLM's self-reported confidence is usually poorly calibrated and overconfident.
Confidence is therefore computed from evidence, with self-assessment as a
deliberately weak input.

### `heuristic-v1` (Phase 2; selectable with `scoring.version: heuristic-v1`)

```text
evidence_coverage   0.30   comments, constraints, FKs, keys, profile (yes/no)
schema_coherence    0.25   name/type agreement, role fit, FK consistency
hypothesis_margin   0.20   top hypothesis vs alternatives
counter_evidence    0.15   critique pass that tries to falsify the meaning
model_self_report   0.10   the model's own confidence
```

Its flaws, found in the Phase 3 review and verified by running the scorer: being
profiled adds +0.075 whatever the data shows (a contradicted meaning scores
*higher* once profiled), and an uncommented plain column cannot exceed 0.775,
i.e. can never be accepted. Details in §23.

### `heuristic-v2` (Phase 3B — implemented 2026-11-14, the configured default)

Every piece of evidence is a ledger item `{kind, ref, direction
supports|contradicts, strength, source}`, produced deterministically wherever
possible.

```text
evidence_support   0.35   noisy-OR(supporting) × (1 − noisy-OR(contradicting))
coherence          0.20   claim-vs-fact checks: catalog checks + profile checks
counter_evidence   0.20   critique verdict (the critique sees the evidence too)
hypothesis_margin  0.15
model_self_report  0.10
```

Properties (with the default strengths, which are placeholders until Phase 6
calibration): names alone can never reach `accept_at` (ceiling 0.650); generic
consistency is not enough; specific evidence (a detector match, a passed
inclusion test) is; a contradiction lowers a score below the no-evidence
baseline. **Coverage** — the deepest stage reached — is stored separately, so a
low score is never confused with "we did not look". A deterministic
`semantic_type` is stored alongside.

Raw and calibrated scores, the weights version and the full ledger are persisted
with every scored version, so score changes are explainable and Phase 6 can fit
a calibrator per model on SME-reviewed examples.

### Bands

| Score | Meaning | Action |
|---|---|---|
| `>= 0.80` | Well supported | Accept |
| `< 0.80`, stages left | Under-evidenced | Escalate to the next stage |
| `< 0.80`, stages exhausted | The machine has run out of means | SME review queue, prioritised by uncertainty × importance |
| `< 0.45` | Abstain | Do not present as fact; queue for SME first |

Retrieval (Phase 4) uses every non-abstain meaning, ranked by score, with the
uncertainty disclosed (R2).

---

## 13. Chat-to-SQL pipeline

`retrieve → plan → generate (tsql) → validate (sqlglot) → execute (read-only) → repair (≤2) → answer`

1. **Classify** — answerable / ambiguous / needs clarification / out of scope.
2. **Retrieve** — hybrid search over semantic descriptions, names, synonyms and
   glossary terms; pgvector similarity + trigram + expansion along FK edges;
   include only the most relevant tables/columns.
3. **Plan** — entities, measures, filters, time grain, grouping, ordering,
   required joins, clarification need.
4. **Generate** — target-dialect SQL plus explanation, structured output.
5. **Validate** — single statement; `SELECT`/`WITH` only; identifiers resolved
   against the catalog; join paths must exist as FK edges or confirmed hints;
   reject `xp_cmdshell`, `OPENROWSET`, `OPENDATASOURCE`, `BULK INSERT`,
   four-part names, `EXEC`, temp-table writes; inject `TOP (n)` when no limit.
6. **Execute** — read-only transaction, isolation `READ UNCOMMITTED`,
   `SET LOCK_TIMEOUT`, driver timeout, row cap.
7. **Repair** — bounded retries (2), feeding the DB error and relevant schema
   context back to the model; thinking mode off for latency.
8. **Answer** — natural language + SQL + result rows + assumptions +
   confidence, plus the pinned semantic version and snapshot.

Safety is enforced by the read-only login **and** by the application; prompts are
never the only guardrail.

---

## 14. Caching: Redis **and** LLM prompt caching

Both, for different reasons.

**Redis** (ephemeral only):

```text
llm:{model}:{prompt_version}:{sha256(prompt+params)}
embed:{model}:{sha256(text)}
semantic:column:{column_id}:{structure_hash}:{prompt_version}:{model}
answer:{target}:{semantic_version}:{sha256(normalized_question)}
```

Profiles are **not** cached in Redis and profiling locks are PostgreSQL advisory
locks (P3-10): PostgreSQL already stores every profile, and a second copy of
sample-derived data buys nothing.

- No embedding-similarity answer caching in v1 — too easy to serve a subtly
  wrong answer. Exact normalized match only.
- Every key carries the structure hash and semantic version.
- Suggested TTLs: LLM 30 d, embeddings 30 d, chat answers 5–15 min.
- Versioned key prefixes instead of mass `KEYS` scans.
- Redis must be safe to flush without losing semantic knowledge.
- If row-level security is ever introduced, tenant/role must be part of every
  business-result cache key.

**Provider prompt caching** is the larger win where available (DeepSeek): keep
the system prompt, schema block and few-shot examples byte-identical at the
front, append only the question. This constrains prompt templating — no
timestamps, no random IDs, no per-request reordering in the prefix — and that
constraint is enforced structurally in the prompt builder. Note Ollama does not
offer an equivalent, so the Ollama provider path trades cost for latency/cache
behaviour.

---

## 15. Embeddings (local Ollama)

| Model | Dims | Size | Notes |
|---|---|---|---|
| `bge-m3` | 1024 | ~1.2 GB | **chosen** — multilingual, 8k context |
| `nomic-embed-text` | 768 | ~274 MB | English, fast |
| `mxbai-embed-large` | 1024 | ~670 MB | strong English |

Client details:

- `POST http://localhost:11435/api/embed` with `{"model": "bge-m3", "input": [...]}`
  (batch supported), or `/api/embeddings` for single inputs.
- `num_gpu: 0` keeps embeddings on CPU: the 4 GB GPU already holds
  `qwen2.5-coder:1.5b` (~2.3 GB) and embedding traffic would thrash it.
- Surface `model not found` as a clear "run `ollama pull bge-m3`" error.
- Retry with backoff; cache by `sha256(text)` in Redis.
- From a containerised client the host would be `host.docker.internal:11435`.
- Dimensions are baked into `vector(1024)`; changing models means an explicit,
  tracked re-embed, recorded via `embedding_model` on each row.

---

## 16. Repository layout

```text
gaia/
├── docker-compose.yml
├── Makefile
├── README.md
├── OUTLINE.md                  # this file
├── .env.example
├── .gitignore
├── config/
│   └── gaia.yaml
├── docs/measurements/           make measure reports kept as evidence
├── infra/
│   ├── postgres/init/001_extensions.sql     metadata store
│   ├── sqlserver/001_readonly_login.sql
│   └── pg-target/                           PostgreSQL dev target (D25)
│       ├── schema.sql, readonly_role.sql
│       └── seed.py
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   └── src/gaia/
│       ├── main.py
│       ├── api/            chat.py, targets.py, semantics.py, review.py, health.py
│       ├── core/           config.py, registry.py, logging.py, security.py
│       ├── db/             engine.py, models/, repositories/ (snapshot, semantics,
│       │                   profile stores)
│       ├── dialects/
│       │   ├── base.py         MetadataReader / Profiler / SqlGuard protocols
│       │   ├── registry.py     dialect name -> implementation (lazy), probes
│       │   ├── bootstrap.py    built-in dialects: mssql, postgres (alias postgresql)
│       │   ├── types.py        per-dialect type vocabularies
│       │   ├── probe.py        check-target result type
│       │   ├── errors.py       StatementTimeout / LockTimeout (3.0)
│       │   ├── mssql/          connection.py, catalog.py, probe.py, measure.py,
│       │   │                   profiler.py (3A; guard.py in 4)
│       │   └── postgres/       the same
│       ├── introspect/     snapshot.py, hashing.py, diff.py
│       ├── profiling/      summary.py (evidence contract, 3B); budget.py, statistics.py,
│       │                   detectors.py, identities.py, joins.py, lineage.py,
│       │                   service.py (Phase 3A)
│       ├── semantics/      reasoner.py, prompts/ (v1, v2), scoring.py, evidence.py,
│       │                   chunking.py, review.py, embeddings.py, calibration.py (Phase 6)
│       ├── query/          retrieval.py, planner.py, generator.py, validator.py, executor.py, answerer.py
│       ├── llm/            base.py, deepseek.py, replay.py, structured.py, cache.py, embeddings.py
│       ├── jobs/           worker.py, tasks.py
│       └── cache/          redis.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/Review.tsx, Chat.tsx
│   │   ├── components/     MessageList, ResultTable, SqlViewer, ConfidenceBadge
│   │   ├── api/client.ts
│   │   └── i18n/           index.ts, locales/en.json
└── tests/
    ├── unit/
    └── integration/
```

`dialects/` is the forward-compatibility seam: `MetadataReader`, `Profiler` and
`SqlGuard` are protocols; `mssql` and `postgres` implement them (readers, probes
and profilers today; guards in Phase 4).

The owner's existing `gaia-env/` is the backend runtime; it is the single Python
environment and is excluded from version control.

---

## 17. Delivery phases

> Phase-by-phase scope, acceptance criteria, open decisions and approval gates
> are maintained in **[PHASES.md](PHASES.md)**. The table below is the summary.

| Phase | Scope | Done when |
|---|---|---|
| **0** | Compose stack (pg18+pgvector, redis), extensions, `gaia-env` provisioned, config loading, FastAPI `/health`, Alembic baseline, `make check-target` | metadata DB + Redis healthy; `check-target` passes against the restored SQL Server |
| **1** | MSSQL catalog reader, canonical JSON snapshot, three-hash hashing, `catalog.*` tables, incremental diff + tombstoning | repeated introspection is idempotent; a comment-only change is detected **— DONE, see §22** |
| **2** | LLM client (DeepSeek + Ollama), JSON-mode structured outputs with Pydantic retry, table + column reasoning, evidence-based scoring, embeddings, `semantics.*` | every column has meaning, evidence, alternatives, score breakdown **— DONE, see §23** |
| **3** | *Approved 2026-11-14:* 3.P PostgreSQL target + dialect (**done**, §25); module/view evidence, bounded profiling, detectors, targeted verification, scoring v2, one-pass re-reasoning, SME review queue | accuracy on a name-blind worst-case simulation improves and the score ranks correct above incorrect |
| **4** | Retrieval (all non-abstain meanings, ranked), planner, SQL generation per dialect, `sqlglot` guards, read-only execution, repair loop, streaming API, data-source management | golden business questions return correct SQL and results |
| **5** | Web UI: **SME review first**, then chat (R1) | an SME works through a database's queue; a question is answered in the browser |
| **6** | Evaluation harness, calibration from SME labels per model, cost/latency tracking | repeatable regression report |
| **7** | Masking/FPE, scheduled refresh, further dialects, workload evidence | later |

Phase 1 is gated on the owner restoring the target database.

---

## 18. Risks and guardrails

1. **LLM overconfidence** → evidence-based scoring, critique pass, calibration, abstention.
2. **Sensitive sample data in prompts** → masked for remote models, raw only for local models (D24); sample opt-out; no-network mode later.
3. **Production database load** → replica, timeouts, sampling, budgets, off-peak runs.
4. **Huge schemas exceeding context** → reason per table bundle, never the whole schema at once.
5. **Schema drift** → structure hash + cache invalidation; `modify_date` is not trusted.
6. **Unsafe SQL** → read-only login, AST validation, single-statement enforcement, timeouts, row caps.
7. **Cache leakage** → structure hash, semantic version and (later) tenant/role in every key.
8. **Confidence mistaken for truth** → expose the score as an estimate with its components, never as a fact.
9. **JSON-mode gaps** → schema validation, retry on blank/invalid content.
10. **Wrong embedding model** → a coder model produces meaningless vectors; pin `bge-m3` and record dimensions.
11. **Target container fragility** → non-root uid 10001, `shm-size`, removable-media permissions.

---

## 19. Open questions

All three original questions are resolved: the reasoning model is a per-customer
configuration (local or remote; development uses the DeepSeek gateway, D1/A7);
the target database is `nge_small`; the embedding model is `bge-m3` (D2). Open
items are tracked in PHASES.md ("Open items").

---

## 20. Phase 0 checklist

1. [x] Project skeleton directories
2. [x] `OUTLINE.md`
3. [x] `docker-compose.yml` (pg18+pgvector :5433, redis :6380)
4. [x] `infra/postgres/init/001_extensions.sql`
5. [x] `.env.example`, `.gitignore`, `config/gaia.yaml`, `.env` (generated secrets)
6. [x] `backend/pyproject.toml` + installed into `gaia-env`
7. [x] FastAPI `/health` + `/api/meta` with `${VAR}` config loading
8. [x] Alembic baseline (`0001`, applied; `alembic current` = head)
9. [x] `Makefile` incl. `check-target`, `check-embed`, `check-env`
10. [x] Stack up + all six extensions and six schemas verified
11. [x] `ollama pull bge-m3` + embedding smoke test (1024 dims, PASS)
12. [x] `make target-info` / `check-target` implemented and failure-path verified
13. [x] Dialect seam (`MetadataReader`/`Profiler`/`SqlGuard` protocols + registry)
14. [x] Unit tests (10 passing)
15. [x] `check-target` passes against the restored SQL Server (see §21)

## 21. Verified Phase 0 results

| Check | Result |
|---|---|
| `docker compose ps` | `gaia-metadata-db` healthy, `gaia-redis` healthy |
| Extensions | `vector 0.8.7`, `pg_trgm 1.6`, `unaccent 1.1`, `btree_gin 1.3`, `pgcrypto 1.4`, `pg_stat_statements 1.12` |
| Schemas | `catalog`, `profiling`, `semantics`, `chat`, `evaluation`, `audit` |
| `GET /health` | `{"status":"ok"}` with `metadata_db.ok` and `redis.ok` true |
| `GET /api/meta` | redacted config, no unresolved placeholders |
| Alembic | `upgrade head` → `0001 (head)` |
| `check_embeddings` | `bge-m3`, 1024 dims, PASS |
| `check_target` | **all 7 checks PASS** — see below |
| `check_llm` | plain, JSON mode, thinking, usage — all PASS |
| `pytest` | 20 passed |

### Target database (verified live)

| Property | Value |
|---|---|
| Database | `nge_small` |
| Server | SQL Server 2025 `17.0.4065.4` (RTM-CU7) |
| Login | `gaia_ro` (read-only, proven by DDL rejection) |
| Objects | 68 tables, 3 views, **607 columns** |
| Schemas | `batch`, `dbo`, `dw`, `finance`, `hr`, `rpt` (views), `web` |
| Total rows | 156,804,374 |
| Table comments | **0** |
| Column comments | **0** |

**Zero `MS_Description` comments.** As anticipated, statistics and sample data
will carry essentially all of the evidentiary weight, and most columns will
initially score low. Deep profiling is therefore the core of Phase 3, not a
top-up.

### LLM endpoint (verified live)

| Property | Value |
|---|---|
| Base URL | `https://evren-llmapi.ssyz.org.tr/v1` (OpenAI-compatible gateway) |
| Model id | `deepseek-v4.1-flash` (routed to `deepseek/deepseek-v4.1-flash`) |
| Context | 1,048,576 tokens; 65,536 default max output |
| Capabilities | tools, thinking, `reasoning_effort`, `response_format`, vision |
| Pricing | free until 2026-12-01 (credit balance reported per call) |

Integration findings that shaped the client:

1. **Chain of thought arrives as `reasoning`**, not the documented
   `reasoning_content` — this gateway uses the OpenAI field name. The parser
   accepts both.
2. Thinking is toggled with `{"thinking": {"type": "enabled"|"disabled"}}` and
   ignores `temperature`; the client omits temperature in thinking mode.
3. **Latency cost of thinking is large**: ~9 s without, ~42 s with, for a small
   column-classification task. Phase 2 must therefore keep thinking enabled only
   for semantics/critique and disabled for bulk work.
4. Usage reports `reasoning_tokens`, cached tokens, and an `evren` block with
   remaining credits — captured in `Usage` for cost accounting.

### Target provisioning fixes discovered

Two real defects were found and corrected while wiring the target:

1. **`DENY CONTROL` silently blocks CONNECT.** `CONTROL` at database scope
   implies `CONNECT`, so denying it locked `gaia_ro` out entirely (Msg 916).
   `infra/sqlserver/001_readonly_login.sql` now denies only
   `INSERT, UPDATE, DELETE, ALTER, REFERENCES`.
2. **`localhost` fails, `127.0.0.1` works.** The container publishes only on
   IPv4, so an ODBC client resolving `localhost` to `::1` stalls and raises
   `HYT00 Login timeout expired`. Config now uses the literal IPv4 address.

Also fixed: an orphaned database user (SID mismatch after login recreation),
repaired with `ALTER USER ... WITH LOGIN`.




## 22. Phase 1 results (complete)

### Implemented

| Module | Purpose |
|---|---|
| `introspect/hashing.py` | Canonical JSON, versioned `structure`/`data`/`profile` hashes |
| `introspect/snapshot.py` | Canonical snapshot document; deterministic, sorted, hash-bearing |
| `introspect/diff.py` | `ChangeKind`, `ObjectChange`, `diff_snapshots`; localized column diffs |
| `introspect/service.py` | read → diff → apply pipeline; document rehydration |
| `dialects/mssql/catalog.py` | 9 set-based `sys.*` queries; hardened single-cursor session |
| `dialects/base.py`, `registry.py`, `bootstrap.py` | Dialect seam and built-in registration |
| `db/models/catalog.py` | 7 models: data_sources, snapshots, tables, columns, constraints, relations, indexes |
| `db/repositories/snapshot_store.py` | Upsert + tombstone + child replacement |
| `tools/introspect.py` | CLI with `--dry-run`, `--json`, `--target` |
| `alembic/versions/0002_catalog.py` | Migration (applied) |

### Verified live against `nge_small`

| Check | Result |
|---|---|
| First capture | 71 objects (68 tables, 3 views), 607 columns, 93 FKs, 166 indexes |
| Second capture | `unchanged=71`, **tables written 0, columns written 0** |
| Determinism | two reads → byte-identical 304 KB document, identical hashes |
| **Comment-only change** | `structure_changed=1` on `dbo.transactions`, `needs reasoning: 1`, `columns written: 0`, `data_hash` unchanged |
| `modify_date` after comment | **unchanged (2026-10-26)** — proves content hashing is required |
| Comment reverted | `structure_hash` returned to its original value |
| Tombstones | 0 |
| Row counts | match `sys.dm_db_partition_stats` (e.g. 26,220,475) |
| Store contents | 1 data source, 9 snapshots, 71 tables, 607 columns, 119 constraints, 93 relations, 166 indexes |
| `pytest` | 51 passed |

### Bugs found and fixed during Phase 1

1. **`collation` is a reserved word in PostgreSQL.** `CREATE TABLE` failed with
   `syntax error at or near "collation"`. Renamed to `collation_name`.
2. **`snapshot_from_document` dropped `modification_counter` and
   `stats_updated_at`.** Rehydrated tables hashed differently from live reads, so
   every table reported `data_changed` on the second capture while the snapshot
   `data_hash` was identical — a contradiction that exposed it. Both fields are
   now restored, with a regression test.
3. **`Snapshot.data_hash` used the structure-hash salt.** Wrong version tag;
   per-table and snapshot hashes disagreed. Now uses `data_hash()`.
4. **Table structure payload was column-order-sensitive.** Reordering columns
   registered as a structural change with zero changed columns — confusing and
   would have cost an LLM call. Columns are now sorted by name.
5. **pyodbc lost the statement handle when a second cursor was created** while
   the first was unreferenced: `fetchone()` silently returned `None`. Replaced
   with a single-cursor `_session()` context manager.
6. **`_scalar` lost its `return`** during an edit — `server_version` and
   `database` silently became `"None"`. Caught by inspecting real output.

### Design notes worth keeping

- The catalog is **always** re-read in full. It costs seconds; reasoning costs
  minutes. Skipping the read would risk missing comment edits, which
  `modify_date` does not expose.
- `structure_hash` excludes row counts, sizes and stats dates, so data movement
  never forces re-reasoning. `data_hash` covers only those signals and
  invalidates profiles alone.
- Changing the hash payload shape requires bumping `STRUCTURE_HASH_VERSION`.
  That intentionally triggers one re-baseline write pass; the next capture is a
  no-op again.
- Constraints, relations and indexes are deleted-and-reinserted per table on
  structure change; columns are upserted and tombstoned individually because
  semantic versions reference them.

### Known limitations (deferred)

- A pure column reorder does not refresh stored `ordinal` values, because it is
  deliberately not a structural change.
- `sys.sql_modules` (view/procedure definitions) is not captured; view semantics
  are inferred from columns and joins only.
- Sequences, synonyms, and security predicates (row-level security) are not read.



## 23. Phase 2 implementation and results (complete)

Built against PHASES.md Phase 2 and decisions D12–D18, then verified live on
2026-11-13 against PostgreSQL 18 + pgvector, SQL Server 2025 `nge_small`, the
DeepSeek gateway and Ollama `bge-m3`. All acceptance boxes that can be checked
in this phase are ticked; two were deferred (one needs a test, one belongs to
Phase 4). Committed as `3585eba`.

### Modules

| Module | Purpose |
|---|---|
| `llm/structured.py` | JSON mode → Pydantic → caller checks → one corrective retry with the exact errors; every call returned as a `CallRecord` (raw payload kept); truncation fails fast |
| `llm/cache.py` | Redis response cache `llm:{model}:{prompt_version}:{sha256}`; degrades to a miss if Redis is down |
| `llm/embeddings.py` | Ollama `/api/embed` client: batching, `num_gpu`, dimension + NaN checks, retries, Redis cache `embed:{model}:{sha256(text)}` |
| `semantics/bundle.py` | Table bundle (PHASES list) with stable evidence refs + per-column catalog facts; data-source object list |
| `semantics/schemas.py` | Response models for reasoning and critique; coverage checks (every column exactly once) |
| `semantics/prompts/` | `reason_table.v1.md`, `critique_table.v1.md`; version = `name.vN+sha256[:10]` of the file |
| `semantics/reasoner.py` | One reasoning call + one critique call per table; join-hint resolution |
| `semantics/critique.py` | Critique prompt; sees claims, not the reasoner's confidence or evidence |
| `semantics/scoring.py` | Five-component score, breakdown, bands |
| `semantics/service.py` | Scope, skip-by-hash, concurrency, versioned persistence, embedding, report |
| `db/models/semantics.py`, `db/models/audit.py` | `reasoning_run`, `table_semantic`, `column_semantic`, `join_hint`, `glossary_term`, `embedding`; `audit.llm_call` |
| `db/repositories/semantics_store.py` | Versioned writes, stale marking, FK/LLM join-hint upserts, embedding targets |
| `tools/reason.py` | CLI: `--schema --table --with-neighbours --limit --no-thinking --dry-run --show-bundles --force --no-embed --no-cache --cost-report` |
| `alembic/versions/0003_semantics.py` | Migration (generated from the models, applied and downgraded on PG 16) |

### Prompt layout (provider prefix cache)

```text
[system] reasoning prompt                       byte-identical for every call
[user]   object list: every table/view + its PK  identical within a run
         table bundle (json)                      the only per-table part
```

The object list exists so the model can suggest joins for reference-like
columns with no declared FK. Suggestions are kept only if every column and
the target resolve in the catalog and they do not repeat a declared FK. The
list is not part of the skip key, so adding a table elsewhere re-reasons
nothing.

### Evidence refs

Constraints, indexes and FKs carry refs (`pk:`, `uq:`, `ck:`, `ix:`, `fk:` +
name). Generic refs: `name`, `type`, `nullability`, `default`, `comment`,
`identity`, `computed`, `table_name`, `table_comment`, `sibling_columns`,
`row_estimate`. A cited ref is valid only if it exists *and* concerns the
column (e.g. `default` only if the column has one). Check-constraint columns
are recovered from the bracketed identifiers in the definition, because the
Phase 1 reader does not store `parent_column_id`. Changing the reader would
have changed every structure hash.

### Scoring as implemented

```text
evidence_coverage  0.30  Σ evidence_signals over catalog facts ONLY:
                         comment .30, foreign_key .15, key .10, check .10,
                         default .05, identity/computed .05, profile .25 (Phase 3)
                         untrusted/disabled FK or check earns half
schema_coherence   0.25  mean of applicable checks: role↔type, FK role, FK domain,
                         single-column PK role, check↔value domain, unit↔role,
                         cited refs valid (fraction; none cited = 0)
hypothesis_margin  0.20  (c − a) / c; a = best alternative, or 0.2 if none listed
counter_evidence   0.15  critique verdict: supported 1.0, plausible .7,
                         doubtful .35, contradicted 0, not assessed .5
model_self_report  0.10  the model's confidence
```

All values are in `scoring` in `config/gaia.yaml`. Contributions are rounded
to 6 decimals before summing, so the breakdown sums exactly to `raw_score`.
The calibrator is the identity function. Without comments or profiles, the
other four components can contribute at most 0.70. An uncommented identity PK
with perfect answers therefore scores 0.745, and a plain column scores at most
0.70, both below `accept_at` (0.80). Only a rare column carrying FK + key +
check + default together could pass 0.80 (maximum 0.835). A missing critique
also blocks `auto_approved`. This is the honest
expectation in PHASES: the low scores become Phase 3's work queue.

The table score is the mean of its column scores; the table's own
self-reported confidence is stored separately.

### Versioning and staleness

* Skip key per table: `structure_hash` + reasoning prompt version + critique
  prompt version + model. Row counts are not in it (OUTLINE §10).
* Each reasoning inserts version N+1 for the table and all its columns.
  Partial unique indexes keep exactly one `is_current` row per table and per
  column.
* At the start of each run, current rows whose table structure changed, or
  whose table or column was tombstoned, are marked `stale`. A failed
  re-reasoning therefore never leaves outdated meaning looking current.
* Embeddings attach to a semantic *version*: a re-reasoned column gets a new
  vector, and the old one stays with the old version.

### Deviations from earlier text, and why

* The Ollama client is `llm/embeddings.py`, as PHASES lists it.
  `semantics/embeddings.py` only builds the text to embed.
* Two additions: `semantics.reasoning_run`, so the cost report can be rebuilt
  and query runs can pin a version, and `audit.llm_call` from §8, for raw
  payloads.
* `llm.timeout_s` was added (600 s when Phase 2 shipped, raised to **1800 s** in
  the addendum). Non-streaming thinking calls send
  nothing until they finish, and the previous hard-coded 300 s could cut off
  wide tables.

### Live verification results

| Check | Result |
|---|---|
| Scope run | `--table dbo.transactions --with-neighbours` → 10 tables |
| Neighbour selection | matches the FK graph exactly: 6 outgoing + 3 incoming for `dbo.transactions`, plus 1 via a shared parent; **one hop only** |
| Dry run | `would_reason=10`, zero LLM calls, zero writes |
| Real run | 10 tables, 99 columns, 109 embeddings (1024-d), 94 join hints (93 FK + 1 inferred) |
| Re-run | `reasoned=0 skipped=10 failed=0`, `calls=0`, 0.3 s |
| Breakdown integrity | 99/99 breakdowns sum exactly to `raw_score` |
| Score distribution | mean 0.616, median 0.622, range 0.360–0.713 |
| Bands | accepted 0, deep_profile 65, deep_profile_flagged 32, abstain 2 |
| Tests | 131 passed, 1 skipped |

Interpretation, stated plainly: **no column was accepted, and that is correct
here.** With zero `MS_Description` comments, `evidence_coverage` is ~0.10
(`comment=0, profile=0, fk=0`), so the four non-evidence components cap the
score near 0.70 by construction — exactly the ceiling documented above. These
columns become Phase 3's queue.

Inference quality on `dbo.transaction_lines` (catalog facts only): `line_tax` →
"Line Tax Amount", `quantity` → "Quantity Sold", `unit_price` → "Unit Price",
`condition` → "Item Condition". Model self-confidence ran above the evidence
score throughout (0.90 vs 0.713 on one column), which is the overconfidence the
weighting exists to correct.

### Defects found in live verification

1. **`llm.concurrency` equalled the gateway's hard ceiling.** The gateway
   rejects the 5th simultaneous request per model with HTTP 429 and states the
   limit is 4. With `concurrency: 4`, a retry had no slot to land in and failed
   deterministically. Now **3**.
2. **Transient capacity errors shared the hard-failure retry budget.** 429/503
   were retried 3× with `1.5^n` ≈ 2.25 s, expiring before the gateway recovered.
   Now a separate transient class (`429/502/503/504`): 8 attempts, 60 s cap,
   jitter, `Retry-After` honoured, base 2.0 s. Jitter matters because fixed
   sleeps make concurrent workers retry in lockstep and collide again.

Measured gateway limits for the record: 4 concurrent requests per model, 1200
requests and 2.5M tokens per window, 15M per day. Concurrency, not volume, was
the binding constraint.

### Known limitations / for review

* **Wide bundles and thinking mode — resolved.** `llm.timeout_s` is now 1800 s
  and read timeouts retry once rather than three times, so the widest bundle has
  headroom. The provider is slow by nature; re-assess on provider change.
* **`thinking` is not in the skip key — deliberate.** The default path always
  reasons with thinking, so only an explicit `--no-thinking` can store a
  non-thinking answer. `table_semantic.thinking` records which, and `--force`
  upgrades such a table. See PHASES D22.
* One-column-change isolation is exercised only at table granularity; a
  dedicated test that a single column edit re-reasons exactly one bundle is
  still missing.
* Approved `glossary_term` rows are not yet fed into the bundle; doing so needs
  the glossary state in the skip key.
* View semantics rely on column names only (`sys.sql_modules` not captured) —
  addressed by Phase 3A (module capture and view lineage, A8).
* The trigram indexes in `0003` were not executed in the sandbox (no
  `pg_trgm` there); they are present in the real store. *Executed in a sandbox on
  2026-11-14 (PostgreSQL 16 + pgvector 0.6.2 + contrib), §25.*
* Found in the Phase 3 review: the `profile` evidence signal is a yes/no flag, so
  profiling raises every score by 0.075 regardless of content, and an
  uncommented plain column is capped at 0.775 (< `accept_at`). Replaced by
  `heuristic-v2` in 3B (§12).
* Target connections come from `config/gaia.yaml` today. Runtime, admin-supplied
  connections are a Phase 4 concern — see §24.

---

## 24. Data source management (admin-supplied connections)

> **Built in Phase 4F (2026-11-16, D65)** — as designed below, with application-level
> AES-GCM (P4-6) and the HTTP side switched off until there is authentication. What was
> built and verified: §37 and PHASES, "4F results". The section is kept as the design record.

Raised during the Phase 2 addendum: administrators will add database
connections through the UI, so targets cannot live in `config/gaia.yaml`
forever. This section records the design so Phase 3 does not harden an
assumption we already know will change.

### Two different things, easily confused

| Connection | Direction | Where it lives today | Status |
|---|---|---|---|
| **Metadata store** | Gaia → PostgreSQL 18 | `GAIA_METADATA_URL` in `.env` | Fine as-is; single, operational, not user-facing |
| **Target** | Gaia → SQL Server | `targets.*` in `config/gaia.yaml` | **Must move to the database** |

The current design already opens two distinct connections with two distinct
drivers (`asyncpg` for metadata, `pyodbc` for the target). That part was never a
gap and does not change.

The real gap is **N targets, added at runtime by admins, with credentials that
cannot live in a file an operator edits**.

### Why this is contained rather than a rewrite

Phase 1 and Phase 2 were built around `data_source_id` from the start:

* `catalog.data_sources` already has `name`, `dialect`, `database_name`,
  `server_version`.
* Every dependent row carries `data_source_id` — `tables`, `snapshots`,
  `table_semantic`, `column_semantic`, `join_hint`, `glossary_term`,
  `reasoning_run`.
* `run_introspection(..., target_name=...)` and `--target` already accept a name.
* `settings.target(name)` already resolves by name rather than assuming one.

So adding targets is an **addition**, not a migration of meaning. That was
deliberate, and it is why this is Phase 4 work and not an emergency.

### What has to change (Phase 4)

1. **Credential storage.** `catalog.data_sources` gains an encrypted secret:
   either `pgp_sym_encrypt` with a master key from `.env`, or application-level
   AES-GCM with the key outside the database. Encryption at rest, not just
   access control.
2. **A `TargetResolver`.** Reads targets from the database and returns a
   `TargetConfig`; `config/gaia.yaml targets` becomes a bootstrap seed (useful
   for development and for the first admin). Config and database must not
   silently disagree — config-seeded rows are marked as such.
3. **Onboarding probe.** The per-dialect probes (`dialects/<d>/probe.py`, behind
   `tools/check_target.py`) become the "test connection" action in the admin UI.
   Both probes prove read-only from permissions as well as by a DDL attempt
   aimed at the weakest schema, and accept only a permission error as proof
   (PostgreSQL since 3.P, SQL Server since 3.0). This matters more than it looks: today read-only is
   *guaranteed by provisioning*. With admin-supplied credentials we cannot assume
   it, so read-only has to be **proven** — and that probe already attempts a DDL
   statement and asserts it fails.
4. **Per-target limits.** Statement timeout, lock timeout, max sample rows,
   concurrency and pool size become per-target settings rather than globals.
5. **Audit.** Who added or changed a connection, and when. `audit` already exists
   for LLM calls; this is a second table there.

### Non-negotiables

* Credentials are never logged, never returned by an API, never echoed in an
  error body. `/api/meta` already redacts the password; the same rule applies to
  every new endpoint.
* **Read-only is proven at onboarding and re-proven periodically.** A connection
  that fails the probe is stored as unusable rather than trusted.
* Every cache key and every `query_run` keeps carrying `data_source_id` /
  `snapshot_id`. Multi-target makes a missing key a cross-tenant data leak, not
  just a wrong answer.
* Deleting a data source **tombstones** it; history stays resolvable, exactly as
  with tables in Phase 1.

### What we deliberately do not do now

* No admin UI, no credential API, no migration `0004` for secrets. Phase 3 is
  profiling and is unaffected: it profiles whichever single target is configured.
* No attempt to make the current config path multi-tenant. It stays the
  bootstrap path.

The cost of deferring is low precisely because the keys already exist. The cost
of *not* recording the decision would be discovering, in Phase 5, that the UI
needs credentials the schema has nowhere to put.

---

## 25. Phase 3.P — PostgreSQL development target and dialect (complete in sandbox)

Owner request of 2026-11-14 (PHASES A9): before 3.0, create a PostgreSQL target
with test data and replicate the SQL Server dialect for it. Verified in a
sandbox (PostgreSQL 16.15 target; PostgreSQL 16 + pgvector metadata store);
the Compose service and PostgreSQL 18 are verified on the real host.

### The fixture (`infra/pg-target/`)

A synthetic retail database, ~3 M rows at scale 1, generated deterministically
(`seed.py --seed 42 --scale 1`, 96 s; two runs give identical checksums). It is
shaped for the worst-case premise rather than for realism alone:

| Feature | Where | Tests what |
|---|---|---|
| Meaningless names, no keys between tables, dates as `yyyymmdd` integers, `'Y'/'N'` flags, numbers as text | `legacy.tb_mst01`, `tb_trx02` (`f1…f10`), `tb_prm03`, `xx_aux` | name-free inference; detectors; parse rates; unused columns |
| Business names only as view aliases | `rpt.v_legacy_transactions` | alias back-propagation (A8); exact ground truth for 3C |
| Computed view columns and aggregates | `rpt.v_customer_overview`, `v_daily_sales`, `mv_product_margin` | computed-column reasoning; materialized views |
| Undeclared references | employee ids, store codes, customer codes, `tb_prm03` lookup codes | join-candidate discovery, inclusion tests (98.9 %) |
| Arithmetic identities | `total = subtotal + tax + shipping`, `f7 = f5 × f6`, generated `line_subtotal` | identities as evidence for measures |
| Untrusted / disabled constraints | `NOT VALID` FK with orphans, `NOT VALID` check with violations, FK triggers disabled with orphans | trust flags; why trusted FKs need no orphan test and others do |
| Enum, domain, identity, serial, generated, partitioned table, expression / partial / INCLUDE / GIN indexes | throughout | catalog mapping |
| Checksum-valid synthetic identifiers | TCKN, IBAN, card BIN + last four, reserved e-mail domains | detectors with validators |
| Exactly three comments | one table, two columns | comment reading without lifting the worst case |

### The dialect (`backend/src/gaia/dialects/postgres/`)

Mapping decisions are documented in `catalog.py`; the important ones: partitions
fold into their parent; materialized views are `view`; domains render as their
base type with the domain and its checks in `type_definition`, enums keep their
name with the labels there; identity *and* serial columns are `is_identity`;
stored generated columns are `is_computed`; `NOT VALID` is `is_not_trusted`; an
FK whose enforcement triggers are disabled is `is_disabled`; sessions pin
`search_path = pg_catalog` so rendered catalog text — and every hash — is
independent of the role's search path.

The scorer's type checks now use per-dialect vocabularies (`dialects/types.py`):
PostgreSQL `timestamp` is a date-time while SQL Server's is a row version. An
oracle test proves the SQL Server vocabulary reproduces Phase 2 exactly, and a
golden test proves SQL Server snapshot hashes and rendered bundles are
byte-identical, so no stored SQL Server semantics re-reason.

Results and the eight defects found and fixed (among them: a DDL probe in one
safe schema proves nothing; `default_transaction_read_only` is not a boundary;
one unresolved placeholder anywhere failed every `check-target`) are in PHASES,
"3.P results".

---

## 26. Phase 3.0 — preconditions (code complete; SQL Server runs pending)

### What changed in the design

* **Timeouts are typed and enforced on both dialects.** Every target session
  raises `StatementTimeout` / `LockTimeout` (`dialects/errors.py`), so a
  budgeted profiler can degrade one metric instead of failing a table. SQL
  Server's statement timeout — configured since Phase 0 — is applied for the
  first time.
* **Read-only is proven from permissions on both dialects**, and a live DDL
  attempt aimed at the weakest schema counts only if it fails with a permission
  error. On SQL Server `EXECUTE` on a procedure is treated as a write path
  (ownership chaining).
* **Measurement is a tool, not a one-off.** `make measure` re-runs the cost
  model on any target (pages, cold/warm, spills, footprint, sampling support).
  Every new deployment can size its budgets from its own numbers.

### What the PostgreSQL numbers changed

The 2026-11-13 conclusion "exact profiling is affordable (10–30 s per table)"
came from SQL Server, a unique-key distinct count and a warm cache. On
PostgreSQL at 6.7 M rows, exact profiling of every column costs 28–60 s per
table and extrapolates past the 180 s ceiling at 24 M rows; exact distinct
counts spill to temp; the cheapest exact form differs by column (`GROUP BY` vs
`count(DISTINCT)`); the planner's distinct estimate is only good enough to pick a
strategy. The staged design (§11) already anticipated this — exact for small
tables, one exact batched scan, everything else by per-metric strategy within a
page budget — and 3A now has measured inputs for it (PHASES, "3.0 results").

---

## 27. Phase 3B — reasoning with evidence (complete in sandbox)

Built before 3A at the owner's request (D29). The design point that made that
possible: **reasoning never reads the target**. It consumes a
`TableEvidenceInput` per table (`profiling/summary.py`) — profiles, detector
hits, identities, view aliases, verification tests — and 3A's only job is to
produce that contract. Until it does, evidence is catalog-only, or supplied as
JSON for tests.

What a v2 reasoning call sees: the Phase 2 bundle, plus an evidence block with
refs the model may cite. What it returns: the Phase 2 answer plus a
`semantic_type` from a controlled vocabulary. What happens next is
deterministic: every fact becomes a ledger item that supports or contradicts
the claim (a name never does), `heuristic-v2` scores the ledger, the ledger is
stored, and columns the machine cannot settle go to `semantics.review_item`
ranked by `(1 − score) × importance`.

Three rules keep cost proportional to change:

* the **evidence hash** joins the skip key, but covers only stable facts — so a
  table re-reasons when its evidence means something new, not on every insert;
* a version whose **critique failed** is re-run until it has one;
* **wide tables** are chunked by column under the context and output budgets, so
  local models with small windows work.

Data values reach a remote model only as shapes where they could be personal or
free text; a local model sees them raw (D24).

The replay provider lets a person — or another system — act as the model
through files (D30). The assistant used it to run the v2 prompts end to end on
the PostgreSQL target; that run is a functional test only (the assistant
designed that database), and it found five defects that unit tests had not
(PHASES, "3B results").

---

## 28. Phase 3A — evidence collection (complete in sandbox; SQL Server runs pending)

The producer side of the 3B contract. One command per stage, both dialects:

```text
make introspect → make profile → make reason → make profile-tests → make reason
                  (Stage 1)       (Stage 2)     (Stage 3)             (only tables whose
                                                                       evidence changed)
```

**Stage 1** profiles every table that is not catalog-sufficient, within page and
time budgets, on one read-only session: a batched exact scan, one row-sized
sample processed in memory (detectors, identities, granularity, values for join
tests), exact or labelled-approximate distinct counts and top values. Views are
not sampled; their definitions are parsed into lineage, and their aliases —
including single-column conversions — become evidence for the base columns.
Join candidates come from values, never names, and are tested by row-weighted
inclusion against the parent key. A second run inside the freshness window reads
no data at all.

**Stage 3** derives tests from what the model claimed (a join hint → inclusion in
that parent; an identifier → exact uniqueness; a code or flag → exact top-k),
runs only those that have not run, and changes the evidence of exactly the
tables concerned.

Every number carries how it was obtained (`method`, rows scanned, pages, time,
isolation, seed). Nothing here ever fails a run for one bad statement: the metric
degrades and says why. Results on `demo_pg`, the corrections the build forced
(dense integer keys, legacy orphans, biased value samples, coincidental codes,
conversions as aliases, a ceiling a statement could outlive) and the deferred
items are in PHASES, "3A results".

**The limit that matters for the worst case:** an integer reference without a
declared foreign key, into a dense surrogate key, cannot be confirmed from data —
every integer in range is found. Such joins rest on aliases, names and the model,
and stay in review until an SME or a stronger source (Query Store, backlog)
confirms them.

**Owner verification (2026-11-15).** The first runs on the real host (PostgreSQL 18,
SQL Server 2025, the configured model) confirmed the pipeline and found what the
sandbox could not: model join hints that pointed code columns at integer
surrogate keys (the object list showed only primary keys) and then errored in
Stage 3 instead of failing; hints that never expired; a specificity rule that
let a count "pass" into a 97 %-dense id; an FK that contradicted a correct
`code`; and a columnstore scan that meets the statement timeout. The join-hint
and evidence fixes, and the lifecycle of model hints (`inferred` → `stale` /
`refuted`), are recorded in PHASES (D38–D42).

The owner's decisions of the same day changed three working rules (D43–D48):
a table is **re-reasoned** only when the model would see something new and
otherwise **re-scored** from its stored answer (`make rescore` never calls the
model); **pass-through view columns inherit** their base column's meaning and
never reach the queue; **blank text is NULL**. The profiler now samples first,
reads columnstore column by column, batches the low-cardinality top-k of large
tables in one `GROUPING SETS` scan, says why a table is `partial`, and pairs
tables profiled in earlier runs with parents profiled later. On FKs (D40): a
declared FK and a passed inclusion test are one fact — the values are another
table's keys — read the same way in fact and transaction tables; they support
`reference`, `identifier` and `code`, are neutral to `dimension`, `flag` and
`timestamp`, and contradict `measure` and `free-text`. Nothing depends on FKs
being declared.

---

## 29. Phase 3C — evaluation (harness built; runs pending)

The question 3C answers is whether the evidence makes the meanings *right* — not
whether it makes scores higher. `nge_small` has honest names, so it simulates
the worst case: a name-blind world replaces every identifier with an opaque
token and rewrites every definition that mentions one, leaving the data as it
is; the hidden names become the reference, and the owner's labels replace them
where a name would be no fair reference. Five conditions — names visible or
hidden × catalog only or full Phase 3, plus thinking off — run the production
pipeline (including each condition's own Stage 3) without writing to the
metadata store; a judge compares each answer with its reference; AUROC asks
whether the score ranks right answers above wrong ones; the owner audits a fifth
of the judgements without seeing them. On `demo_pg` the native worst case has an
exact ground truth, with and without the view that names the legacy columns.
Details and the decisions taken while building it: PHASES, "3C harness", D51.

**Owner runs (2026-11-15/16).** B-C completed (99 columns, 20 calls); B-P stopped
on a 14-column table whose answer was truncated — thinking had used 12,437 of
the 16,000 output tokens — and a B-C critique had failed without a word. A
truncated answer is now asked again in smaller column chunks (D53), failed
critiques are reported, and two conditions are compared on the columns both
answered (D55). The run resumes from its recordings (PHASES, "Owner runs of §12
and §13b").

---

## 30. Dialect policy (D52, owner, 2026-11-16)

PostgreSQL is the main dialect. The reason is speed of verification, not
preference: the assistant can reproduce a PostgreSQL target and metadata store
end to end, while every SQL Server result needed a round trip through the owner
(days, in practice). From now on a dialect change is built and accepted on
PostgreSQL first; the SQL Server counterpart follows with tests, and what only
the owner can run is written to OWNER_VERIFICATION and answered when it can be.

What does **not** become optional (refinements proposed with D52): the offline
T-SQL tests, which run anywhere; anything about safety — a write path or a guard
bypass on SQL Server stops its use until fixed; and the honesty of accuracy
claims — `demo_pg` was designed by the assistant, so it proves the machinery,
while meaning accuracy is measured on `nge_small` or on a realistic PostgreSQL
copy of it.

**D56 (owner).** SQL Server is verified asynchronously on delivered builds: every
delivery is a commit with a build marker (`make stamp`), the owner runs the SQL
Server track on the latest build when time allows, and each result is read
against the build it names; fixes land in later builds.

The slowest loop is still the reasoning model (~15 tokens/s through the gateway;
a table with thinking takes 2–15 minutes). D52 shortens the database loop; it
does not shorten that one, which is why Phase 4's generation runs with thinking
off and why every evaluation run is resumable.

---

## 31. Phase 4A — the SQL guard (done in the sandbox)

Before Gaia generates any SQL, it has the only door SQL can go through. A
statement is parsed with `sqlglot` and checked against allowlists — a single
`SELECT`/`WITH` query; functions by the name the database will see — and against
Gaia's own store, never the live database: every object must be in the catalog
snapshot (so no system catalog is ever readable), every column must resolve, and
every join must be backed by a join path. Join paths are key classes built from
declared FKs, model hints whose claim test passed (D48), SME hints and view
lineage, so two facts that reference the same key join as readily as a parent and
its child, while a join invented between unrelated columns is refused. A row cap
is added or lowered. Then — the design's central rule — what runs is the guard's
**rendering** of the tree it validated, never the text it was given: anything the
parser misread can only become something harmless. The rendering is parsed and
checked once more, executed in the dialect's read-only session with the statement
timeout, and recorded with the snapshot and the semantic versions it read.

The guard is the second layer, not the first: the read-only login and session
stay the boundary (D4), and a statement forced past the guard in the tests still
meets "read-only transaction" (PostgreSQL) or a permission error (SQL Server).
Since D58 the guard also refuses joins that multiply rows: besides a join path,
one side of every join must be unique (a key, or a derived table grouped by the
join column) — the fan trap, `order_lines ⋈ payments ON order_id`, is the commonest
silent error of generated SQL, and view lineage is an alias, not a join.

Its suites — 78 + 46 refused statements (fan traps included since D58), 29 accepted, on PostgreSQL and T-SQL —
pass with every sqlglot major from 25 to 30; where an old parser differs it
refuses, never accepts. Decisions: D57. Details: PHASES, "4A results".

---

## 32. Phase 4B — retrieval (done in the sandbox)

A question becomes a schema context: the objects it needs, what Gaia believes their
columns mean and how sure it is, the values and date ranges that make filters
writable, and the only joins allowed. Two channels find the objects. The lexical one
needs nothing but the store: BM25 over names split into words (with `qty`, `trx`,
`cust`… expanded), meanings, synonyms and the readable values of low-cardinality
columns — so it works without the embedding service and on objects never reasoned,
where names are all there is. The vector one compares the question with the
`bge-m3` embeddings of the current meanings. Ranks are fused, columns roll up to
their objects, and confidence weights the result without ever filtering it (R2):
an uncertain meaning is used and said to be uncertain; an abstained one is not used.

The objects found are connected by the guard's own join paths — declared FKs first,
then through views, then tested model hints — with bridge objects added on the way,
and only joins the guard accepts are offered, so the generator cannot be led into a
join it will then be refused, nor into a fan trap. What reaches a remote model
follows the exposure policy: enum labels, small code sets and date ranges, never
values of personal data.

On `demo_pg`, with meanings derived from names and no vectors, every expected object
was in the context for all 22 test questions and joined there for 19; the other three
need joins that exist only as model hints. Real numbers need real meanings and
`bge-m3` on the owner's host: `make retrieval-eval` is the Phase 2 "retrieval smoke
test" made a tool. Decisions: D58, D59. Details: PHASES, "4B results".

---

## 33. Phase 4C — answering (done in the sandbox)

A question now ends in an answer. Retrieval builds the context; one model call decides
whether the question can be answered as asked and, if so, writes the query with what it
computes, what it assumes and the columns it relies on; the guard checks it; it runs
read-only; a refusal, a database error or a timeout goes back to the model in the same
conversation, twice at most; and a second call writes a few sentences about the result.
The user sees a table and that narrative (P4-2) — the SQL is kept for audit, not shown.

The owner's rule for ambiguity is *clarify, always* (P4-4): two readings that give
different numbers, a relative period outside the data, a join that is not there — the
answer is a question back, with options. Relative periods are written as dates computed
from "today", so every answer can say which period it means and every run can be
repeated. Nothing the narrative model sees includes values of personal data when the model
is remote. Every answer carries its interpretation, its assumptions, the unconfirmed
meanings it relies on and a confidence that says why it is what it is.

In the sandbox the pipeline was run against the real guard, database and store with a
scripted model, through every path; the golden set of `demo_pg` — 21 questions with
reference SQL, 4 that must be clarified — passes the whole pipeline with its reference
answers; and a replayed run showed the loop working as intended: a fan-trap join refused,
explained, corrected, and the golden number returned. What the configured model makes of
all this is the next measurement (4D). Decisions: D60. Details: PHASES, "4C results".

---

## 34. Phase 4D — golden runs, and clarifications remembered (done in the sandbox)

A golden run asks every question of a set as a user would — clarifying where the set says
the user would — and scores the final answer by its result, not its SQL: two right queries
rarely look alike, two right results always hold the same values. Columns are matched by
their values, numbers at the precision shown, rows tied at a "top ten" cut are
interchangeable. The Phase 4 targets — executable for 80 %, correct for 70 % — are read from
such a run on the owner's host.

The owner's own questions changed two things before any model saw them. They are phrased
the way people ask — "top 5 shops performing best last year" — and each is ambiguous in a
way that changes the numbers, so each is a clarification test. And they exposed the limits
of word matching: "shops" found nothing in a database of "stores", and "the best supplier in
2025" needs sales and dates the question never names. A short list of business synonyms and
two structural rules — a ranking needs the activity behind it, a period needs the dated facts
— took the names-only coverage of those questions from four in seven to six in seven.

The owner also proposed that Gaia learn from clarifications (D61). Gaia now records, with
every question it asks back, the answer it would have given: the option it would choose and
that option's query, checked but never shown or run. The user's choice is recorded beside it.
What that makes possible is a measurement before any behaviour change: how often, for a kind
of ambiguity on a data source, Gaia's choice is the user's. Only when that is measured high
enough — on users' clarifications, not test runs — would Gaia answer such a question without
asking, and then still say what it assumed and offer the other readings; what it learns is a
definition, confirmed by an SME before it applies to everyone. Decisions: D61–D63. Details:
PHASES, "4D results".

---

## 35. Phase 4E — the chat API (done in the sandbox)

Questions become conversations. A turn streams its progress — found the data, writing the
query, checking it, running it — and the table goes out the moment it exists, before the
narrative, which at the gateway's pace can take tens of seconds more. An open clarification
is answered by simply replying ("1"), and when that reply is the option Gaia would have
chosen, its stored best guess answers at once. A follow-up ("and in 2024?") is read with the
questions before it and what was computed for them. What is kept of an answer is enough to
show it again and never personal data: a bounded copy of the table with personal-data columns
masked, and no SQL. Decision D64; details: PHASES, "4E results".

---

## 36. Learning from clarifications: methods considered (owner question, 2026-11-16 — not a decision)

D61 records, for every clarification, the option Gaia would have chosen and the one the user
chose. That is a labelled dataset produced as a side effect of clarifying always — and, while
Gaia always asks, an unbiased one: no answer was withheld because Gaia was sure. What can be
learned from it, in increasing order of data needed:

1. **A proportion with an honest interval** (per ambiguity kind and data source). The agreement
   rate is a binomial proportion; its 95 % lower bound (Beta posterior) is what a ≥ 90 % rule
   should compare: 27/30 is an observed 90 % but a lower bound of 0.77; 30/30 gives 0.91; 95 %
   observed needs ~100–200 answers to clear 0.90. A hierarchical (partially pooled) version
   shares strength across kinds and data sources, so a new data source starts from the others'
   rate instead of from nothing. Tens of answers suffice.
2. **Selective prediction with a guarantee.** Treat "answer without asking" as a classifier
   that may abstain; choose its threshold on held-out clarifications so that the error rate
   among the questions it answers stays below a target (risk-coverage curve). Conformal
   prediction makes the guarantee distribution-free: answer directly only when the calibrated
   prediction set holds a single option. Hundreds of answers.
3. **A learned chooser.** Features the store already has — the question's `bge-m3` embedding,
   the options' embeddings, the ambiguity kind, retrieval scores, who asks — into a
   logistic model or gradient-boosted trees, calibrated like the scores of Phase 6 (isotonic).
   Or case-based: the nearest past clarification by embedding, and its answer, given to the
   model in the prompt (in-context learning). Hundreds to a few thousand answers.
4. **Neural models.** A cross-encoder fine-tuned to score (question, option) pairs, or a reward
   model of users' preferences. Thousands of answers per population, ideally pooled across
   customers — which their data separation usually forbids — and their gain over 3 is mostly in
   paraphrase robustness, which the embeddings of 3 already provide.

What "more precise" means here is not top-1 accuracy — the model's best guess already is the
top-1 — but **coverage at a fixed error rate**: how many questions can be answered without asking
while wrong answers stay under, say, 5 %. Methods 1–2 deliver that with the data a customer will
realistically produce; 3 adds generalisation across paraphrases; 4 is unlikely to pay before a
large multi-tenant dataset exists. Once Gaia starts answering without asking, the data stops
being unbiased (the user only corrects what they notice): keeping a small random share of
questions asked anyway — exploration, as in contextual bandits — keeps the measurement honest.

---

## 37. Phase 4F — data sources added at runtime (done in the sandbox)

§24's design, built. `config/gaia.yaml` stays the bootstrap path; any other name is looked up
in the metadata store, so every existing command and the API work with an added data source
without knowing where it came from (`TARGET=<name>`). What makes it safe to let an
administrator type in a connection:

* **The password is encrypted in the application** (AES-256-GCM) with a key that lives only in
  `.env` (`GAIA_MASTER_KEY`), bound to its row — copied onto another data source, it does not
  decrypt. A second key variable makes rotation possible without downtime
  (`make datasource ARGS="rotate-key"`). The password is never returned, logged, audited or
  put in an error.
* **Read-only is proven, not assumed.** A new data source is unusable until the same
  nine-check probe as `make check-target` passes — privileges, and a write that must fail — and
  becomes unusable again after any change or when its last proof is older than 30 days. A
  superuser's credentials fail it.
* **Nothing is deleted.** Removal tombstones the row, wipes the secret and frees the name; the
  snapshots, meanings and runs that point at it stay readable.
* **Every change is audited** (`audit.connection_change`: who, what, which fields — never
  values).

The administration API exists but is off (`datasources.admin_api`) until Phase 5 brings
logins (P5-1); until then it is a CLI for whoever holds the server. Decision D65; details:
PHASES, "4F results".

