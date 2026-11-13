# Gaia — Phase Plan (review & control)

> **Purpose.** A checkable plan for what gets built, in what order, and what
> must be true before each phase is considered finished. Use this to approve,
> reorder, or cut scope **before** work starts.
>
> This document is the control surface. `OUTLINE.md` is the design rationale;
> this file is the delivery contract.
>
> Last updated: 2026-11-13 · Status: Phase 0 and 1 complete, Phase 2 awaiting approval

---

## How to use this

Each phase has:

| Section | Meaning |
|---|---|
| **Goal** | One sentence: the capability that exists at the end |
| **Deliverables** | Concrete files/modules/artifacts produced |
| **Acceptance criteria** | Checks that can be run, not opinions |
| **Decisions needed** | Choices that need your approval before starting |
| **Estimate** | Effort, and cost/latency where LLM calls dominate |
| **Gate** | A checkbox that must be ticked by you before the next phase starts |

Nothing moves to the next phase with an unticked gate. If a phase is cut, mark
it `CUT` with a reason rather than deleting it, so the reasoning survives.

---

## Status at a glance

| Phase | Scope | State | Gate |
|---|---|---|---|
| 0 | Infrastructure, config, health, target verification | **DONE** | ✅ approved |
| 1 | Catalog introspection, hashing, incremental diff | **DONE** | ✅ approved |
| 2 | Semantic reasoning, evidence scoring, embeddings | **planned** | ⬜ awaiting approval |
| 3 | Deep profiling, PII, re-reasoning | **planned** | ⬜ |
| 4 | Retrieval, T-SQL generation, guarded execution | **planned** | ⬜ |
| 5 | React chat UI | **planned** | ⬜ |
| 6 | Evaluation harness, score calibration | **planned** | ⬜ |
| 7 | SME review, masking, scale-out dialects | **backlog** | ⬜ |

### Environment as it stands

| Component | State |
|---|---|
| Metadata store | PostgreSQL 18 + pgvector on `127.0.0.1:5433`, healthy |
| Extensions | `vector 0.8.7`, `pg_trgm`, `unaccent`, `btree_gin`, `pgcrypto`, `pg_stat_statements` |
| Schemas | `catalog`, `profiling`, `semantics`, `chat`, `evaluation`, `audit` |
| Migrations | `0001` baseline, `0002` catalog — at head |
| Redis | `127.0.0.1:6380`, healthy |
| Target | SQL Server 2025 CU7, `nge_small`, login `gaia_ro` (read-only, proven) |
| Catalog captured | 71 objects, 607 columns, 93 FKs, 166 indexes, **0 comments** |
| Reasoning LLM | `deepseek-v4.1-flash` via gateway, JSON mode + thinking verified |
| Embeddings | Ollama `bge-m3`, 1024 dims, verified |
| Tests | 51 passing |

---

## Phase 0 — Infrastructure · **DONE**

**Goal.** A runnable, verifiable foundation: metadata store, cache, config,
health checks, and a proven read-only path to the target.

**Delivered**

- `docker-compose.yml` — pgvector/pg18 + Redis only; `sql1` and `ollama-code`
  stay external and owner-managed
- `infra/postgres/init/001_extensions.sql` — 6 extensions, 6 schemas
- `config/gaia.yaml` + `.env` with `${VAR}` interpolation and unresolved-name reporting
- `backend/src/gaia/core/config.py`, `db/engine.py`, `main.py` (`/health`, `/api/meta`)
- Alembic baseline `0001`
- `tools/check_target.py`, `check_embeddings.py`, `check_llm.py`
- `Makefile` — up/down/reset-db/venv/check-*/dev-api
- `infra/sqlserver/RESTORE.md` + `001_readonly_login.sql`

**Acceptance — all met**

- [x] `docker compose up` healthy; extensions and schemas present
- [x] `/health` reports `ok` for metadata DB and Redis
- [x] `check-target` passes 7/7 including a proven DDL rejection
- [x] `check-embeddings` returns 1024 dims
- [x] `check-llm` exercises plain, JSON mode, thinking, usage
- [x] `check-target` fails loudly when the target is misconfigured

**Notable defects fixed**

- `DENY CONTROL` implies `CONNECT` — locked `gaia_ro` out entirely (Msg 916)
- `localhost` resolved to `::1`; container publishes IPv4 only → `HYT00`
- Orphaned DB user after login recreation (SID mismatch)

---

## Phase 1 — Catalog introspection · **DONE**

**Goal.** Read the target schema completely and deterministically, store it as
JSONB plus normalized rows, and detect *any* structural change cheaply enough
that re-running is routine.

**Delivered**

- `introspect/hashing.py` — canonical JSON, versioned structure/data/profile hashes
- `introspect/snapshot.py` — deterministic snapshot document
- `introspect/diff.py` — change classification with column-level localization
- `introspect/service.py` — read → diff → apply pipeline
- `dialects/mssql/catalog.py` — 9 set-based `sys.*` queries, single-cursor session
- `dialects/{base,registry,bootstrap}.py` — dialect seam
- `db/models/catalog.py` — 7 models; `alembic/versions/0002_catalog.py`
- `db/repositories/snapshot_store.py` — upsert, tombstone, child replacement
- `tools/introspect.py` — CLI with `--dry-run`, `--json`, `--target`

**Acceptance — all met**

- [x] First capture: 71 objects, 607 columns, 93 FKs, 166 indexes
- [x] Second capture: `unchanged=71`, 0 tables written, 0 columns written
- [x] Two reads produce a byte-identical 304 KB document
- [x] A comment-only change is detected, scoped to 1 table, with `data_hash` unchanged
- [x] `modify_date` demonstrably does not move on comment changes
- [x] Reverting the comment restores the original `structure_hash`
- [x] No tombstones on an unchanged schema
- [x] Row counts match `sys.dm_db_partition_stats`

**Notable defects fixed** — reserved word `collation`; rehydration dropped
data-hash inputs; wrong hash salt; order-sensitive payload; pyodbc cursor
handle loss; a lost `return` statement.

---

## Phase 2 — Semantic reasoning & evidence scoring

**Goal.** Every column in scope has a stated business meaning, the evidence
behind it, the alternatives considered, and an honest score that says how much
to trust it — stored, versioned, and embedded for retrieval.

This is the heart of the product. It is also the phase where a bad design is
most expensive to undo, so the checkpoint matters more than the schedule.

### Deliverables

| Area | Artifact |
|---|---|
| Schema | `alembic/versions/0003_semantics.py`, `db/models/semantics.py` |
| Tables | `table_semantic`, `column_semantic`, `join_hint`, `glossary_term`, `embedding` |
| LLM | `llm/structured.py` — Pydantic-validated JSON with bounded retry |
| LLM | `llm/embeddings.py` — Ollama client, batching, Redis cache |
| LLM | `llm/cache.py` — response cache keyed by prompt hash |
| Prompts | `semantics/prompts/` — versioned, byte-stable prefix for cache hits |
| Reasoning | `semantics/reasoner.py` — per-table bundle reasoning |
| Reasoning | `semantics/critique.py` — counter-evidence pass |
| Scoring | `semantics/scoring.py` — weighted breakdown, raw + calibrated |
| Orchestration | `semantics/service.py` — skip-by-hash, versioning, batch control |
| CLI | `tools/reason.py` — `--schema`, `--table`, `--limit`, `--no-thinking`, `--dry-run`, `--cost-report` |
| Tests | Prompt rendering, schema validation, scoring math, cache-key stability |

### Reasoning strategy

One call per **table bundle**, not per column. 68 tables → 68 primary calls, not
607. Rationale: a column's meaning depends on its table's purpose and its sibling
columns; reasoning per column in isolation produces contradictory answers and
costs an order of magnitude more.

Each bundle contains: table name, comment (usually absent), row estimate, column
list with types/nullability/defaults, PK and unique constraints, check
constraints (these often encode business rules verbatim), indexes, and the
foreign keys **plus the referenced table and column names** — a strong
disambiguation signal.

Per column the model returns: `business_name`, `purpose`, `role` (identifier /
measure / dimension / timestamp / flag / code / free-text / reference), `unit`,
`value_domain`, `synonyms`, `example_questions`, `assumptions`, `unknowns`,
`alternatives`, and a self-reported confidence.

### Scoring (the "honest score")

Computed from evidence, with self-reported confidence weighted lowest:

```text
evidence_coverage   0.30   comments, constraints, FKs, keys, profile depth
schema_coherence    0.25   name/type agreement, role fit, FK consistency
hypothesis_margin   0.20   clear winner vs competing alternatives
counter_evidence    0.15   critique pass that tries to falsify the meaning
model_self_report   0.10   the model's own confidence
```

Both `raw_score` and `calibrated_score` are stored with `score_breakdown`, the
weights version, prompt version and model id. In Phase 2 the calibrator is
identity; Phase 6 fits it against SME-reviewed examples.

The score must be **decomposable**: any number must be traceable to the evidence
that produced it. A score nobody can interrogate is worse than no score.


### Honest expectation for this phase

The target has **zero** `MS_Description` comments across 607 columns. Phase 2
sees only catalog facts, so a large share of columns — especially
`source_system`, `address_hash`, and the `dw.*` aggregate fields — will score
below 0.60. **That is the design working, not failing.** The low scores are the
work queue for Phase 3.

If a run reports high confidence everywhere on this schema, the scorer is lying
and must be fixed before moving on. Judging "good" by average score would invert
the entire point of the project.

### Acceptance criteria

- [ ] Every column in scope has a `column_semantic` row with meaning + breakdown
- [ ] Every table in scope has a `table_semantic` row
- [ ] `score_breakdown` components sum to the stored `raw_score`
- [ ] Re-running with unchanged `structure_hash` issues **zero** LLM calls
- [ ] Changing one column's structure re-reasons only its table bundle
- [ ] Every LLM response validates against the Pydantic schema, or is retried and
      recorded as a failure with the raw payload retained
- [ ] Prompt prefix is byte-identical across calls (cache-hit rate measurable)
- [ ] Every column has a 1024-dim embedding with the model name recorded
- [ ] A cost/latency report exists: tokens in/out, call count, wall time
- [ ] Score distribution is printed and shows real spread, not a uniform band
- [ ] Retrieval smoke test: 10 business questions return the expected tables in
      the top 5

### Decisions needed before starting

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 1 | **Scope of first run** | (a) all 68 tables, (b) one schema, (c) one table + FK neighbours | **(c) then (a)** — validate scoring before committing |
| 2 | **Thinking mode default** | on (better, ~4–5× slower) / off / per-task | **on for semantics, off for critique** |
| 3 | **Sample data in Phase 2** | yes / no | **no** — keeps Phase 3's contribution measurable |
| 4 | **Output language** | English only / English + Turkish | **English only**; `locale` column is ready |
| 5 | **Concurrency** | 1 / 4 / 8 | **4** |
| 6 | **PII detection: here or Phase 3** | here / Phase 3 | **Phase 3** — pattern matching needs sample values |

### Estimate

| Scope | LLM calls | Wall time @ concurrency 4 | Notes |
|---|---|---|---|
| 1 table bundle + critique | 2 | ~1–2 min | validation run |
| `dbo` schema (~27 tables) | ~54 | ~15–25 min | thinking on |
| All 68 tables | ~136 | ~40–70 min | thinking on |

Cost is zero until 2026-12-01 per the gateway's pricing metadata, with a credit
balance reported on every call. Token volume is roughly 400k–900k input and
200k–500k output for a full pass. The first real run replaces these estimates
with measured numbers.

### Risks

| Risk | Mitigation |
|---|---|
| Overconfident scores | Evidence-weighted scoring; distribution is an acceptance check |
| Contradictory meanings within a table | Bundle-level reasoning, one call per table |
| JSON schema drift | Pydantic validation + bounded retry + raw payload on failure |
| Prompt edits silently invalidating the cache | Prompts versioned; cache key includes prompt version |
| Cost surprise on a full pass | Subset first, cost report, then full run |

### Gate

- [ ] **Approved to start Phase 2**, with scope option chosen (decision 1)

---


## Phase 3 — Deep profiling & re-reasoning

**Goal.** Where confidence is weak, go back to the data. Collect statistics and
samples, classify PII, re-reason with the new evidence, and prove that scores
actually improve.

This is the phase that earns the word "agent": it decides *what it does not know*
and goes to find out.

### Deliverables

- `profiling/statistics.py` — column metrics (nulls, distinct, min/max/avg, stddev,
  percentiles, length distribution, temporal granularity, top-k)
- `profiling/sampler.py` — `TABLESAMPLE SYSTEM ... REPEATABLE(seed)`, row caps
- `profiling/fk.py` — child/parent counts, orphan rate, join match rate
- `profiling/pii.py` — pattern + dictionary detection with confidence
- `profiling/budget.py` — per-table and per-run ceilings, graceful degradation
- `profiling/service.py` — trigger rules, ordering by expected information gain
- `db/models/profiling.py`, `alembic/versions/0004_profiling.py`
- Redis cache + distributed locks (`lock:profile:table:*`)
- `tools/profile.py` CLI; re-reasoning wired into `semantics/service.py`

### Trigger rules

Profile a column when: score < `deep_profile_below` (0.60); it is a FK/join
column; its name is ambiguous and it has no comment; or an SME/CLI requests it.
Profile a table when: its PK is absent or its columns are mostly unprofiled.

Ordering is by expected information gain, not alphabetically — a join column with
low cardinality ambiguity outranks a free-text field.

### Safety

Read-only session, `SET LOCK_TIMEOUT`, per-statement timeout, max rows per sample
(200/table, 25 values/column), skip exact `COUNT(DISTINCT)` above a size budget in
favour of `APPROX_COUNT_DISTINCT`, prefer `sys.*` statistics before querying, and
never `ORDER BY NEWID()` on large tables.

### Acceptance criteria

- [ ] Every triggered column has a `column_profile` with `method`, `seed`,
      `rows_scanned`, `runtime_ms`
- [ ] Low-confidence columns show a measurable score increase after re-reasoning;
      report the before/after distribution
- [ ] No profiling query exceeds its statement timeout; degradation is recorded
- [ ] Table with 26M rows profiled within the per-table budget
- [ ] PII classifications stored with evidence and confidence
- [ ] Profiling is idempotent per `data_hash`; unchanged data reuses cached profiles
- [ ] Total profiling wall time and rows scanned are reported

### Decisions needed

| # | Decision | My recommendation |
|---|---|---|
| 7 | Profiling budget per table (default 120 s) | 120 s, degrades to sampling |
| 8 | Is masking required before samples reach the LLM | **yes, mask by default**; raw samples stay local |
| 9 | Sample size per table (default 200 rows) | 200 |
| 10 | Run profiling on all low-score columns or top-N first | **top 50 by information gain**, then re-assess |

### Gate

- [ ] **Approved to start Phase 3** · [ ] masking policy confirmed

---

## Phase 4 — Retrieval, T-SQL generation, guarded execution

**Goal.** Turn a business question into correct, safe SQL over `nge_small`, and
return an answer.

### Deliverables

- `query/retrieval.py` — hybrid: pgvector similarity + trigram + FK expansion
- `query/planner.py` — entities, measures, filters, grain, joins; clarification
  decision when ambiguous
- `query/generator.py` — T-SQL generation with compact schema context
- `query/validator.py` — `sqlglot` (`read="tsql"`) AST guard
- `query/executor.py` — read-only execution, timeouts, row caps
- `query/answerer.py` — natural-language answer from results
- `api/chat.py` — SSE streaming endpoint
- `db/models/chat.py`, `alembic/versions/0005_chat.py`
- `tools/ask.py` CLI

### Validation rules (non-negotiable)

Single statement; `SELECT`/`WITH` only; identifiers resolved against the catalog;
join paths must exist as FK edges or confirmed hints; reject `xp_cmdshell`,
`OPENROWSET`, `OPENDATASOURCE`, `OPENQUERY`, `BULK INSERT`, `EXEC`, four-part
names, temp-table writes; inject `TOP (n)` when no limit exists.

Safety is enforced by the read-only login **and** the application. Prompts are
never the only guardrail.

### Acceptance criteria

- [ ] A golden set of ~20 business questions returns executable SQL for ≥80%
- [ ] Zero statements outside `SELECT`/`WITH` reach execution, including adversarial
      prompts (enforced by test, not by inspection)
- [ ] Every `query_run` pins `snapshot_id` and semantic version
- [ ] Execution respects timeout and row cap; violations are recorded
- [ ] Repair loop bounded at 2 attempts; failures surfaced, not hidden
- [ ] Ambiguous questions produce a clarification request instead of a guess
- [ ] Latency and tokens recorded per run

### Decisions needed

| # | Decision | My recommendation |
|---|---|---|
| 11 | Golden question set: who authors ground truth | **you supply ~20 real business questions**; I write the expected SQL and you correct it |
| 12 | Answer style: numbers only, or narrative + table | narrative + table + SQL |
| 13 | Default row limit | 1000 |
| 14 | Clarify vs best-guess on ambiguity | **clarify** |

### Gate

- [ ] **Approved to start Phase 4** · [ ] golden questions available

---

## Phase 5 — React chat UI

**Goal.** Ask a question in a browser and see the answer, the SQL, and the result.

### Deliverables

- Vite + React + TypeScript + pnpm; i18next with `en` locale
- `api/client.ts`, SSE consumption
- Components: `MessageList`, `SqlViewer`, `ResultTable`, `ConfidenceBadge`
- Single chat screen with streaming, error states, clarification prompts

### Acceptance criteria

- [ ] End-to-end question answered in the browser against `nge_small`
- [ ] Streaming renders progressively; no full-page reloads
- [ ] SQL is visible and copyable; result table scrolls and formats numbers/dates
- [ ] Confidence badge shows score **and** breakdown on hover/expand
- [ ] No hard-coded English strings outside locale files
- [ ] `pnpm build` succeeds; dev server on `127.0.0.1:5173`

### Gate

- [ ] **Approved to start Phase 5**

---


## Phase 6 — Evaluation & calibration

**Goal.** Make quality measurable and regressions visible, and turn the
heuristic score into a calibrated probability.

### Deliverables

- `db/models/evaluation.py`, `alembic/versions/0006_evaluation.py`
- Golden question suite with expected SQL and expected result shape
- Column-purpose test set with SME-agreed labels
- Calibration: fit isotonic/Platt on reviewed examples; store the calibrator
  version beside the score
- `scripts/evaluate.py` producing a repeatable report
- Regression diffing between runs (score changes, SQL changes, pass/fail deltas)

### Acceptance criteria

- [ ] One command produces a report: execution accuracy, column-purpose accuracy,
      calibration error, mean latency, tokens, cost
- [ ] Report is reproducible run-to-run on an unchanged schema
- [ ] Calibration improves over identity baseline, measured, not asserted
- [ ] A deliberate prompt regression is caught by the suite

### Decisions needed

| # | Decision | My recommendation |
|---|---|---|
| 15 | Calibration needs reviewed examples — how many are acceptable | ~150 reviewed columns as a first fit |
| 16 | Which metric is the primary gate | **execution accuracy** on golden questions |

### Gate

- [ ] **Approved to start Phase 6**

---

## Phase 7 — SME review, masking, scale-out (backlog)

Not scheduled. Listed so scope creep has somewhere to land.

- SME review queue + UI; `status` transitions already exist in the schema
- PII masking applied to samples and answers, not just detected
- Scheduled incremental refresh
- Additional dialects (PostgreSQL first — the seam is ready)
- Multi-tenant access control and row-level-security awareness in cache keys
- View/procedure definitions from `sys.sql_modules`

---

## Cross-cutting concerns

| Concern | Position |
|---|---|
| Secrets | `.env` only; never in YAML, logs, or the metadata DB |
| Safety | Read-only login + AST validation + timeouts + row caps; prompts never the only guard |
| Reproducibility | Every run pins snapshot id, semantic version, prompt version, model |
| Determinism | Hashing, snapshot serialization and diffing are canonical and tested |
| Caching | Redis is ephemeral; PostgreSQL is the source of truth; every key carries a version |
| Cost | Measured per phase; subset-first runs before full passes |
| Failure | Recorded, not swallowed: raw LLM payloads and DB errors retained |
| Locale | `locale` columns from day one; English content only for now |

## Decisions log

Decisions taken so far, for reference. Amend here rather than editing history.

| # | Decision | Value |
|---|---|---|
| D1 | Reasoning LLM | `deepseek-v4.1-flash` via OpenAI-compatible gateway |
| D2 | Embeddings | Ollama `bge-m3`, 1024 dims, CPU |
| D3 | Target | SQL Server 2025, `nge_small`, external container `sql1` |
| D4 | Target provisioning | Owner restores; Gaia never creates or alters the DB |
| D5 | Read-only login | Owner creates; Gaia verifies only |
| D6 | Metadata store | PostgreSQL 18 + pgvector, `127.0.0.1:5433` |
| D7 | Cache | Redis, ephemeral only, `127.0.0.1:6380` |
| D8 | Backend runtime | Host process in `gaia-env`, not containerised |
| D9 | Frontend | React + TypeScript + Vite + pnpm, i18next from the start |
| D10 | Catalog re-read | Always full; cheapness recovered via hashing |
| D11 | Target host format | Always `127.0.0.1`; never `localhost` (IPv6 resolution breaks ODBC) |

## Definition of done (per phase)

A phase is done only when all of the following hold:

1. Every acceptance criterion is ticked, and each tick corresponds to a command
   that was actually run — not to code that merely exists
2. Tests cover the new logic, and the full suite passes
3. `OUTLINE.md` records the verified results and any defects found
4. A short summary states what was built, what was proven, and what was deferred
5. Your gate is ticked

## Open questions blocking Phase 2

1. **Scope of the first reasoning run** — one table + neighbours (recommended),
   one schema, or all 68 tables?
2. **Thinking mode for semantics** — on (recommended, ~4–5× slower, better) or off?
3. **Any objection to catalog-only inputs in Phase 2**, deferring all sample data
   to Phase 3?

Answer those three and Phase 2 can start without further clarification.

