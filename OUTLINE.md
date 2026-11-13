# Gaia — Outline (revised)

> Metadata-driven semantic agent: introspect a database, reason about what its
> metadata *means*, score that reasoning honestly, deepen the evidence where the
> score is weak, and answer business questions in natural language by generating,
> validating and executing SQL.
>
> Status: design agreed, Phase 0 in progress.
> Metadata store: PostgreSQL 18 + pgvector. First target: SQL Server 2025.
> Reasoning LLM: DeepSeek `deepseek-flash` (config-switchable to local Ollama).
> Embeddings: local Ollama `bge-m3`. Frontend: React + TypeScript (Vite, pnpm).

---

## 1. Product summary

Two connected pipelines.

### A. Background semantic modelling

1. Connect to a target database with a **read-only** account.
2. Extract catalog metadata into a versioned canonical JSON snapshot:
   schemas, tables, views, columns, data types, nullability, defaults,
   computed/identity flags, PK/unique/check constraints, foreign keys, indexes,
   table and column comments, partitions, row estimates, sizes.
3. Run a **cheap reasoning pass** over catalog facts plus existing DB statistics.
4. Produce a structured semantic model per table and per column: business name,
   purpose, role, entity, unit, value domain, synonyms, PII likelihood, join
   hints, example business questions, evidence.
5. Compute an **evidence-grounded confidence score** — not merely the model's
   self-reported confidence.
6. If a column scores below threshold, trigger **deep profiling** against the
   target database.
7. Re-reason with the enriched evidence and store the improved model.
8. Persist everything in Gaia's own PostgreSQL store, with JSONB and pgvector.
9. Queue the weakest items for later SME (human-in-the-loop) review.

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
| Project path | `/home/murat/g120s/gaia` |
| Python env | `gaia-env` (pre-created by owner, Python 3.12.3) |
| Host ODBC | `msodbcsql18 18.4.1.1`, `unixodbc-dev`, gcc 13.3 → `pyodbc` builds natively |
| Target SQL Server | container `sql1`, external to Compose, `localhost:1433` |
| SQL Server version | 2025 RTM-CU7, 17.0.4065.4 |
| Ollama | container `ollama-code`, `localhost:11435`, v0.17.7, `/api/embed` confirmed |
| Backup source | `/home/murat/Downloads/nge-small-mssql-v0.3.0.bak` (6.86 GB) |

Reserved host ports: `5433` (metadata pg18), `6380` (redis), `8000` (API),
`5173` (Vite). External: `1433` (SQL Server), `11435` (Ollama).

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
| Reasoning LLM | `deepseek-flash` via `LLM_MODEL`; provider abstraction with an `ollama` alternative | Live docs map it to `DeepSeek-V4.1-Flash-1017`; 1M context, 384K output, JSON mode, tool calls |
| Thinking mode | On for semantics + critique, off for SQL repair and classification | Thinking is the default and must be explicitly disabled per task to control latency |
| Structured output | `response_format={"type":"json_object"}` + Pydantic validation + bounded retry | JSON mode guarantees valid JSON, not schema conformance; blank content must be retried |
| Prompt caching | Byte-identical stable prefix, volatile question last | Cache-hit input ≈ 1/50th of cache-miss price — the largest cost lever |
| Off-peak scheduling | Bulk re-reasoning prefers off-peak | Peak = 01:00–04:00 and 06:00–10:00 UTC weekdays; off-peak is half price |
| Embeddings | **Local Ollama `bge-m3`, 1024-dim, `num_gpu: 0`** | No cloud embedding cost; schema text stays on-host; multilingual for future i18n |
| Never embed with a coder model | `qwen2.5-coder` returns 1536 dims of untrained noise | Would silently destroy retrieval quality |
| Target DB | SQL Server 2025 only in v1, behind a dialect adapter | Confirmed first target; other engines later |
| Target provisioning | **Owner restores a backup; Gaia never creates the database** | Vendor DB already carries real schema + comments |
| Read-only login | Created manually by owner; **Gaia verifies, never creates** | Target database stays untouched by the app |
| SQL dialect | `sqlglot` with `read="tsql", write="tsql"` | Required for AST validation of generated T-SQL |
| MSSQL driver | `pyodbc` + host ODBC Driver 18 | Already installed on the host; no container needed |
| Backend runtime | **Host process in `gaia-env`** (not containerised in Phase 0) | Uses the owner's pre-created venv; simplest iteration |
| Metadata store | PostgreSQL 18 + pgvector on **5433** | Not reusing the stopped pg15 `vectordb` container |
| Cache | Redis 8 on **6380**, ephemeral only | Postgres remains the source of truth |
| PII | Detect, classify and store per column in v1; masking deferred | Answer #3 |
| SME review | Schema + `status` states from day one; UI later | Answer #8 |
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
  driver-level `timeout`, `rowcount` caps.

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
- `pyodbc` for SQL Server targets (host ODBC Driver 18)
- **Alembic** migrations
- **Pydantic v2** + `pydantic-settings`
- **httpx** DeepSeek client (OpenAI-compatible) + Ollama client, one interface
- **sqlglot** for T-SQL parsing, AST validation, dialect handling
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

Compose owns only Gaia's own infrastructure. SQL Server and Ollama are external.

| Service | Image | Host port | Notes |
|---|---|---|---|
| `gaia-metadata-db` | `pgvector/pgvector:pg18` | `127.0.0.1:5433` | `PGDATA=/var/lib/postgresql/18/docker`; volume mounts `/var/lib/postgresql` |
| `gaia-redis` | `redis:8-alpine` | `127.0.0.1:6380` | `appendonly no`, `maxmemory-policy allkeys-lru` |

| External | Address | Owned by |
|---|---|---|
| SQL Server 2025 (`sql1`) | `localhost:1433` | owner |
| Ollama (`ollama-code`) | `localhost:11435` | owner |

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

`config/gaia.yaml`:

```yaml
app:
  locale: en
  default_target: demo_mssql

targets:
  demo_mssql:
    dialect: mssql            # registry key -> adapter class
    host: localhost
    port: 1433
    database: ${GAIA_TARGET_DATABASE}
    username: ${GAIA_TARGET_USER}
    password: ${GAIA_TARGET_PASSWORD}
    options:
      encrypt: true
      trust_server_certificate: true
    limits:
      statement_timeout_s: 30
      max_sample_rows: 200
      max_runtime_per_table_s: 120
      isolation: read_uncommitted
    capabilities:
      extended_properties: true
      dmv_row_counts: true
      show_statistics: false

llm:
  provider: deepseek          # deepseek | ollama
  base_url: https://api.deepseek.com
  model: deepseek-flash
  api_key: ${DEEPSEEK_API_KEY}
  thinking:
    semantics: true
    sql_generation: false
  json_mode: true
  max_output_tokens: 16000
  concurrency: 4

embeddings:
  provider: ollama
  base_url: http://localhost:11435
  model: bge-m3
  dimensions: 1024
  num_gpu: 0                  # GPU is occupied by the coder model
  batch_size: 32

cache:
  redis_url: redis://localhost:6380/0
  ttl:
    llm: 2592000
    embedding: 2592000
    profile: 604800
    chat_answer: 900

profiling:
  confidence_threshold: 0.75
  deep_profile_below: 0.60
  review_required_below: 0.45
```

`${VAR}` interpolation is resolved from `.env` at load time by Pydantic
settings. Secrets are never written to YAML or to the database.
`targets.<name>.dialect` resolves through a registry, so adding PostgreSQL or
MySQL later is one new adapter module plus a config block — no schema changes.

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

- `table_profile` — row count, size, method (`exact`/`approximate`), timings
- `column_profile` — JSONB stats, samples, top-k, patterns, FK metrics, PII signals, cost

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

PII is **stored, never applied**, in v1. Masking hooks come later.

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
| `data_hash` | `row_count`, `modification_counter` from `sys.dm_db_stats_properties`, `stats_date` | cached profiles only |
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
- A table-level `row_count` delta beyond a configurable ratio (e.g. 20%)
  invalidates profiles even when `structure_hash` is unchanged.

---

## 11. Deep profiling (T-SQL)

Triggered when any of: score below threshold; ambiguous uncommented name;
FK/join column; repeated failed SQL or user follow-ups; weak table-level
reasoning that column evidence could disambiguate; explicit SME request.

Note: a restored vendor database often lacks `MS_Description` comments, which
pushes more columns below the confidence threshold. Statistics and sample data
therefore carry most of the evidentiary weight, and the profiling budget should
assume wide coverage rather than incidental top-ups.

### Table level

- exact/approximate row count, table size
- PK and uniqueness, partition structure
- update recency if a timestamp exists
- relationship cardinality
- optional workload stats from `sys.dm_db_index_usage_stats`

### Column level

| Metric | Approach |
|---|---|
| Column row count | `COUNT_BIG(*)` with `TABLESAMPLE` on large tables |
| Not-null count / null fraction | `COUNT(col)`, `COUNT_BIG(*) - COUNT(col)` |
| Distinct count | exact `COUNT(DISTINCT col)` under a size budget; `APPROX_COUNT_DISTINCT(col)` above it |
| Numeric stats | `MIN`, `MAX`, `AVG`, `STDEV`, `PERCENTILE_CONT` |
| Text length distribution | `MIN/MAX/AVG(LEN(col))`, length histogram |
| Temporal granularity | `MIN`, `MAX`, datepart-difference distinctness |
| Boolean / low-cardinality distribution | grouped counts |
| Top-k values | `SELECT TOP (@k) col, COUNT(*) ... GROUP BY col ORDER BY 2 DESC` |
| Samples | `TABLESAMPLE SYSTEM (p PERCENT) REPEATABLE (seed)` with row cap; `ORDER BY NEWID()` forbidden on large tables |
| Format / pattern | regex over a bounded sample, pattern frequency |
| PII detection | pattern + dictionary heuristics over the sample, stored with confidence |
| FK cardinality / orphans | child vs parent counts, null-FK count, distinct FK count, `LEFT JOIN ... WHERE parent IS NULL` orphan rate, parent key uniqueness |
| Optional | candidate functional dependencies, correlation with PK/time column |

### Sampling and safety

- Prefer existing statistics before running queries.
- Small tables: full scan. Large tables: `TABLESAMPLE` with `REPEATABLE(seed)`.
- Bounded sample volume: e.g. 50–200 rows per table, capped values per column.
- Profile columns independently; do not assemble complete sensitive rows.
- Mask/redact PII before storing samples or sending them to a model.
- Record `method` (`exact`/`approximate`/`sampled`), `seed`, `rows_scanned`,
  `runtime_ms`, `est_cost`.
- Read-only session, `statement_timeout`, lock timeout, max rows, max runtime per
  table, concurrency limits per target, off-peak scheduling for bulk runs,
  prefer a read replica when available.
- A **budget service** enforces ceilings and degrades gracefully (approximate
  instead of timing out) rather than failing the run.

---

## 12. Honest scoring

An LLM's self-reported confidence is usually poorly calibrated and overconfident.
Confidence is therefore computed from evidence, with self-assessment as a
deliberately weak input.

```text
evidence_coverage   0.30   comments, constraints, FKs, stats, profile depth, sample availability
schema_coherence    0.25   name/type agreement, role fit, join consistency, value-domain fit
hypothesis_margin   0.20   top hypothesis vs alternatives, using thinking mode
counter_evidence    0.15   explicit critique pass attempting to falsify the meaning
model_self_report   0.10   the model's own confidence, low weight by design

calibrated_score = calibrator(raw_score)
```

- Start with configurable heuristic weights.
- Once SME-reviewed examples exist, fit isotonic/Platt calibration.
- Persist **raw and calibrated** scores plus the weights version, so score
  changes are explainable.

### Bands

| Score | Meaning | Action |
|---|---|---|
| `>= 0.80` | Well supported | Accept; use in retrieval |
| `0.60 – 0.79` | Plausible but incomplete | Deep profile, then re-reason |
| `< 0.60` | Weak / ambiguous | Deep profile and flag |
| `< 0.45` | Abstain | Do not present as fact; queue for SME |

Also stored per column: evidence references, assumptions, alternative
interpretations, explicit unknowns, and whether the score is calibrated or
heuristic.

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
profile:column:{column_id}:{profile_hash}
semantic:column:{column_id}:{structure_hash}:{prompt_version}:{model}
answer:{target}:{semantic_version}:{sha256(normalized_question)}
lock:profile:table:{table_id}
```

- No embedding-similarity answer caching in v1 — too easy to serve a subtly
  wrong answer. Exact normalized match only.
- Every key carries the structure hash and semantic version.
- Suggested TTLs: LLM 30 d, embeddings 30 d, profiles 7 d or until the
  structure hash changes, chat answers 5–15 min, locks 10 min.
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
├── infra/
│   ├── postgres/init/001_extensions.sql
│   └── sqlserver/
│       ├── RESTORE.md
│       └── 001_readonly_login.sql
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   └── src/gaia/
│       ├── main.py
│       ├── api/            chat.py, targets.py, semantics.py, review.py, health.py
│       ├── core/           config.py, registry.py, logging.py, security.py
│       ├── db/             engine.py, models/, repositories/
│       ├── dialects/
│       │   ├── base.py         MetadataReader / Profiler / SqlGuard protocols
│       │   ├── registry.py     dialect name -> implementation
│       │   └── mssql/          catalog.py, profiler.py, guard.py, sampling.py
│       ├── introspect/     snapshot.py, hashing.py, diff.py
│       ├── profiling/      budget.py, statistics.py, sampler.py, pii.py, service.py
│       ├── semantics/      reasoner.py, prompts/, scoring.py, calibration.py, embeddings.py
│       ├── query/          retrieval.py, planner.py, generator.py, validator.py, executor.py, answerer.py
│       ├── llm/            base.py, deepseek.py, ollama.py, structured.py, cache.py
│       ├── jobs/           worker.py, tasks.py
│       └── cache/          redis.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/Chat.tsx
│   │   ├── components/     MessageList, ResultTable, SqlViewer, ConfidenceBadge
│   │   ├── api/client.ts
│   │   └── i18n/           index.ts, locales/en.json
└── tests/
    ├── unit/
    └── integration/
```

`dialects/` is the forward-compatibility seam: `MetadataReader`, `Profiler` and
`SqlGuard` are protocols; `mssql` is the only implementation in v1.

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
| **3** | Deep profiling loop, budget guard, sampling, FK cardinality, PII classification, Redis cache + locks, re-reasoning | low-confidence columns measurably improve |
| **4** | Retrieval, planner, T-SQL generation, sqlglot guard, read-only execution, repair loop, streaming API | golden business questions return correct SQL and results |
| **5** | React chat UI (pnpm + Vite), i18next wiring, SQL/result rendering | end-to-end question answered in the browser |
| **6** | Evaluation harness, score calibration, cost/latency tracking | repeatable regression report |
| **7** | SME review UI, masking, scheduled refresh, additional dialects | later |

Phase 1 is gated on the owner restoring the target database.

---

## 18. Risks and guardrails

1. **LLM overconfidence** → evidence-based scoring, critique pass, calibration, abstention.
2. **Sensitive sample data in prompts** → mask by default, sample opt-out, local-model option; no-network mode later.
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

1. **Reasoning LLM scope** — (a) DeepSeek `deepseek-flash` for reasoning + SQL,
   Ollama `bge-m3` for embeddings (current default); (b) no cloud at all, larger
   local model for reasoning; (c) both, switchable for comparison.
2. **Target database name** — to be provided by the owner after the restore.
3. **Embedding model** — `bge-m3` (1024) chosen by default; confirm or switch to
   `nomic-embed-text` (768) for a smaller footprint.

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
15. [ ] `check-target` passes against the restored SQL Server — **blocked on owner restore**

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
* View semantics rely on column names only (`sys.sql_modules` not captured).
* The trigram indexes in `0003` were not executed in the sandbox (no
  `pg_trgm` there); they are present in the real store.
* Target connections come from `config/gaia.yaml` today. Runtime, admin-supplied
  connections are a Phase 4 concern — see §24.

---

## 24. Data source management (admin-supplied connections)

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
3. **Onboarding probe.** `tools/check_target.py` becomes the "test connection"
   action in the admin UI. This matters more than it looks: today read-only is
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

