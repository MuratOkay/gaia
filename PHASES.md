# Gaia — Phase Plan (review & control)

> **Purpose.** A checkable plan for what gets built, in what order, and what
> must be true before each phase is considered finished. Use this to approve,
> reorder, or cut scope **before** work starts.
>
> This document is the control surface. `OUTLINE.md` is the design rationale;
> this file is the delivery contract.
>
> Last updated: 2026-11-14 · Status: Phases 0–2 complete and verified · Phase 3 **approved**; pre-work 3.P (PostgreSQL target + dialect) done; 3.0 next
>
> Precedence where documents disagree (D12′): `config/gaia.yaml` > decisions log > PHASES.md > OUTLINE.md.

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
| 2 | Semantic reasoning, evidence scoring, embeddings | **DONE — verified live** | ✅ approved |
| 3 | Evidence collection, profiling, views, scoring v2, re-reasoning, review queue | **IN PROGRESS** — 3.P done; 3.0 next | ✅ approved 2026-11-14 · sub-gates 3.0 / 3A / 3B / 3C open |
| 4 | Retrieval, SQL generation (T-SQL + PostgreSQL), guarded execution, data-source management | **planned** | ⬜ |
| 5 | Web UI: SME review first, then chat (R1) | **planned** | ⬜ |
| 6 | Evaluation harness, score calibration from SME labels (per model) | **planned** | ⬜ |
| 7 | Masking/FPE, scheduled refresh, further dialects, workload evidence | **backlog** | ⬜ |

### Environment as it stands

| Component | State |
|---|---|
| Metadata store | PostgreSQL 18 + pgvector on `127.0.0.1:5433`, healthy |
| Extensions | `vector 0.8.7`, `pg_trgm`, `unaccent`, `btree_gin`, `pgcrypto`, `pg_stat_statements` |
| Schemas | `catalog`, `profiling`, `semantics`, `chat`, `evaluation`, `audit` |
| Migrations | `0001` baseline, `0002` catalog, `0003` semantics — at head |
| Redis | `127.0.0.1:6380`, healthy |
| Target | SQL Server 2025 CU7, `nge_small`, login `gaia_ro` (read-only, proven) |
| Catalog captured | 71 objects, 607 columns, 93 FKs, 166 indexes, **0 comments** |
| PostgreSQL dev target (3.P) | `demo_pg` on `127.0.0.1:5434` (Compose profile `target-pg`), synthetic, ~3 M rows at scale 1; role `gaia_ro` read-only proven from privileges. Verified in a sandbox on PostgreSQL 16; first run on the real host pending |
| PostgreSQL catalog captured | 28 objects (24 tables, 4 views), 213 columns, 3 comments |
| Reasoning LLM | `deepseek-v4.1-flash` via gateway, JSON mode + thinking verified |
| Gateway limits | 4 concurrent requests per model, 1200 req + 2.5M tokens per window, 15M/day |
| Embeddings | Ollama `bge-m3`, 1024 dims, verified |
| Semantics captured | 10 tables, 99 columns, 109 embeddings, 94 join hints |
| Tests | 211 passed, 2 skipped (Ollama live) — sandbox run with the metadata-store and PostgreSQL-target integration tests enabled |

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
- `infra/sqlserver/001_readonly_login.sql` (a `RESTORE.md` runbook was listed here but
  not kept; the database is already restored — owner decision 2026-11-14; the
  restore commands remain in OUTLINE §4.1)

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

## Phase 2 — Semantic reasoning & evidence scoring · **DONE**

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

### Acceptance criteria — 10 of 11 met

- [x] Every column in scope has a `column_semantic` row with meaning + breakdown
      — 99/99 columns across 10 tables
- [x] Every table in scope has a `table_semantic` row — 10/10
- [x] `score_breakdown` components sum to the stored `raw_score` — 99/99 exact
- [x] Re-running with unchanged `structure_hash` issues **zero** LLM calls
      — measured: `reasoned=0 skipped=10 failed=0, calls=0, 0.3s`
- [ ] Changing one column's structure re-reasons only its table bundle
      — **partial**: scoping is per table bundle and unchanged tables skip
      correctly, but no test yet proves that a single column edit inside a table
      re-reasons that one bundle and nothing else
- [x] Every LLM response validates against the Pydantic schema, or is retried and
      recorded as a failure with the raw payload retained
- [x] Prompt prefix is byte-identical across calls — prompt versions are content
      hashes (`reason_table.v1+2541a66663`), so any edit changes the version
- [x] Every column has a 1024-dim embedding with the model name recorded
      — 109 embeddings, single model, `vector_dims = 1024`
- [x] A cost/latency report exists: tokens in/out, call count, wall time
- [x] Score distribution is printed and shows real spread — mean 0.616,
      median 0.622, range 0.360–0.713, no column above 0.8
- [ ] Retrieval smoke test: 10 business questions return the expected tables in
      the top 5 — **deferred to Phase 4**, where retrieval is built

### Decisions needed before starting

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 1 | **Scope of first run** | (a) all 68 tables, (b) one schema, (c) one table + FK neighbours | **(c) then (a)** — validate scoring before committing |
| 2 | **Thinking mode default** | on (better, ~4–5× slower) / off / per-task | **on for semantics, off for critique** |
| 3 | **Sample data in Phase 2** | yes / no | **no** — keeps Phase 3's contribution measurable |
| 4 | **Output language** | English only / English + Turkish | **English only**; `locale` column is ready |
| 5 | **Concurrency** | 1 / 4 / 8 | **4** |
| 6 | **PII detection: here or Phase 3** | here / Phase 3 | **Phase 3** — pattern matching needs sample values |

### Decisions taken (2026-11-13)

The owner's rule: where documents disagree, **`config/gaia.yaml` and OUTLINE.md
win**; otherwise the recommendations above apply.

| # | Decision | Taken |
|---|---|---|
| 1 | Scope of first run | (c) one table + FK neighbours (`--table X --with-neighbours`), then (a) |
| 2 | Thinking mode | **on for semantics and critique** (`llm.thinking` in config, OUTLINE §3); `--no-thinking` available |
| 3 | Sample data | no; catalog facts only |
| 4 | Output language | English only |
| 5 | Concurrency | 4 (`llm.concurrency`) → **corrected to 3** during verification; see below |
| 6 | PII detection | Phase 3; `pii_*` columns exist and stay NULL |
| — | Reasoning provider | DeepSeek only in development; Claude/Gemini may be evaluated later behind `LLMClient`. **Ollama is the embeddings provider only.** |
| — | `join_hint` | declared FKs = `fk`/`confirmed`; model suggestions resolved against the catalog = `llm`/`inferred`; SME rows never overwritten |
| — | `glossary_term` | table only; filled by the human-in-the-loop step or imported from a company glossary, then treated as ground truth |
| — | Wide tables | no chunking; token efficiency is a go-to-market tuning topic. Truncated output fails clearly with a hint to raise `llm.max_output_tokens` |
| — | Status bands | `auto_approved` at ≥ `accept_at`; otherwise `draft`; below `review_required_below` flagged for SME. Tunable later |
| — | Retrieval smoke test | the "10 business questions" item was a brainstorming artifact; not a gate for Phase 2 |

### Estimate vs measured

| Scope | Estimated calls | Measured | Estimated wall time | Measured wall time |
|---|---|---|---|---|
| 1 table bundle + critique | 2 | 2–3 | ~1–2 min | 2–8 min (thinking on) |
| `dbo` schema (~27 tables) | ~54 | — | ~15–25 min | not run |
| 10 tables (transactions + neighbours) | ~20 | 27 | ~10 min | **~75 min** total across retries |

Cost is zero until 2026-12-01 per the gateway's pricing metadata, with a credit
balance reported on every call. Measured for the 10-table scope: ~30k input and
~33k output tokens across 27 calls.

**The estimate was wrong by roughly 7×** on wall time. The cause is not payload
size but thinking-mode latency: a single critique on a 12-column table took
**495 s**, and `dbo.transactions` needed ~718 s even with thinking disabled.
This is the main input to the Phase 3 planning decision below.

### Risks

| Risk | Mitigation |
|---|---|
| Overconfident scores | Evidence-weighted scoring; distribution is an acceptance check |
| Contradictory meanings within a table | Bundle-level reasoning, one call per table |
| JSON schema drift | Pydantic validation + bounded retry + raw payload on failure |
| Prompt edits silently invalidating the cache | Prompts versioned; cache key includes prompt version |
| Cost surprise on a full pass | Subset first, cost report, then full run |

### Verified live (2026-11-13)

Run against the real stack: PostgreSQL 18 + pgvector, SQL Server 2025
`nge_small`, DeepSeek gateway, Ollama `bge-m3`.

| Check | Command | Result |
|---|---|---|
| Migration | `alembic upgrade head` | `0003 (head)`; 6 `semantics` tables + `audit.llm_call` confirmed by schema query |
| Catalog baseline | `make introspect` | `unchanged=71`, 0 rows written |
| Dry run | `make reason-dry ARGS="--table dbo.transactions --with-neighbours --show-bundles"` | `would_reason=10`; **0 LLM calls, 0 writes** |
| Neighbour selection | compare against `catalog.relations` | exactly 6 outgoing + 3 incoming FKs for `dbo.transactions`, plus 1 hop via a shared parent — **one hop only** |
| Real run | `make reason` | 10 tables / 99 columns reasoned, 109 embeddings, 94 join hints |
| Idempotent re-run | same command again | `reasoned=0 skipped=10 failed=0`, **`calls=0`**, 0.3 s |
| Breakdown integrity | SQL sum check | **99/99** breakdowns equal `raw_score` exactly |
| Embedding dimensions | `vector_dims(embedding)` | 1024, single model |

### Score quality (the point of the phase)

```
mean=0.616  median=0.622  min=0.360  max=0.713
bands: accepted 0 | deep_profile 65 | deep_profile_flagged 32 | abstain 2
```

**Zero columns were accepted, and that is the correct outcome.** The target has
no `MS_Description` comments at all, so `evidence_coverage` tops out near 0.10
(`comment=0, profile=0, fk=0`). Profiling evidence is not earned until Phase 3,
so those columns route there instead of being reported as understood.

Sample from `dbo.transaction_lines`, inferred from nothing but names, types and
keys:

| Column | Inferred meaning | raw score |
|---|---|---|
| `transaction_line_id` | Transaction Line Identifier | 0.704 |
| `line_tax` | Line Tax Amount | 0.620 |
| `quantity` | Quantity Sold | 0.593 |
| `unit_price` | Unit Price | 0.564 |
| `condition` | Item Condition | 0.489 |

Model self-confidence runs consistently **above** the evidence score (0.90 vs
0.713 on the same column). The weighting is doing its job.

### Defects found during verification

1. **Concurrency sat exactly on the gateway ceiling.** The gateway rejects the
   5th simultaneous request per model with HTTP 429, stating the limit is 4.
   `llm.concurrency` was 4, so a retry had no free slot and failed
   deterministically. → **3**.
2. **Transient capacity errors shared the hard-failure retry budget.** 429 and
   503 were treated like hard errors: 3 attempts, `1.5^n` ≈ 2.25 s backoff,
   which expires before a saturated gateway recovers. → separate transient class
   (`429/502/503/504`), 8 attempts, 60 s cap, **jitter** (fixed sleeps make
   concurrent workers retry in lockstep and collide again), `Retry-After`
   honoured, base raised to 2.0 s.

Both fixes are confined to the transport layer; no scoring, prompt or
persistence behaviour changed. Measured gateway limits: 4 concurrent requests
per model, 1200 requests and 2.5M tokens per window, 15M per day. Concurrency,
not volume, was binding.

### Closed in the Phase 2 addendum (2026-11-13)

Both items that were going to be carried into Phase 3 are resolved here.

**1. Wide bundles exceeded the read timeout — resolved by raising it.**

`llm.timeout_s` went from 600 s to **1800 s**. The provider is slow by nature;
the observed worst case for the widest bundle (18 columns) was ~718 s without
thinking, so 1800 s leaves headroom for thinking mode. This will be re-assessed
when the provider changes.

Raising the timeout alone would have tripled the worst-case run time, because a
`ReadTimeout` was retried under the general `max_attempts: 3` budget — up to
3 × 1800 s for a single call. Read timeouts therefore now have their own budget,
`retry.max_attempts_timeout: 2` (one retry), and produce a distinct error message
so a slow provider is not mistaken for an unhealthy gateway:

```
completion timed out after 2 attempt(s) at llm.timeout_s=1800s;
consider a narrower bundle or --no-thinking
```

**2. `thinking` is not in the skip key — accepted, not changed.**

The default workflow always reasons with thinking enabled, so a table is only
ever stored without it when someone deliberately passes `--no-thinking`. Adding
`thinking` to the skip key would force a re-reason of every table on each toggle,
which costs far more than the edge case it protects against.

The consequence is recorded rather than hidden: `table_semantic.thinking` stores
how each answer was produced, and a later thinking-enabled run will still skip a
table that was stored without thinking. Use `--force` to upgrade such a table.

### Gate

- [x] **Approved to start Phase 2**, scope (c) one table + FK neighbours
- [x] **Phase 2 complete** — committed as `3585eba`
- [x] **Phase 2 addendum** — timeout raised, timeout retry budget separated,
      thinking skip-key behaviour documented and accepted

---


## Phase 3 — Evidence collection, profiling & re-reasoning · **approved 2026-11-14 — in progress**

**Goal.** For any database Gaia is pointed at, collect every piece of evidence it
can afford — from metadata, code, statistics and the data itself — reason
**once** with all of it, verify the claims that are still uncertain with targeted
tests, and hand what remains to a human with a ranked queue. Scores must go
**up when evidence supports a meaning and down when evidence contradicts it**,
and the result must be measured against ground truth, not against the score.

This is the phase that earns the word "agent": it decides *what it does not know*,
goes to find out within a budget, and says honestly when it has run out of
means.

### Design premise (owner, 2026-11-14)

`nge_small` is a development fixture, not the design target. Every decision in
this phase is taken for the **worst case**, which is also the common case:

| # | Premise | Consequence for Phase 3 |
|---|---|---|
| A1 | Many databases per deployment; usually **no comments, no data dictionary, possibly meaningless names**. An SME per database labels and annotates through the UI | Evidence must come from structure, code and data, not names. The end product of automation is a **ranked review queue**, not "everything accepted". SME input is the ground truth and the calibration source |
| A2 | PII conformance is out of scope for now. Redaction/FPE applies **only when the reasoning model is remote**; local models see raw values. Dev data is synthetic | An exposure mode per provider, not a global masking policy. Data at rest is not addressed in this phase |
| A3 | A score is required; it must identify evidence. Names alone may not be evidence | Scoring is rebuilt around an **evidence ledger** (supporting *and* contradicting items). Names alone can never auto-accept |
| A4 | Intent was *light reasoning → profiling → deep reasoning*, to spend target and model cost only where needed | Escalation is kept, but re-ordered by cost per unit of evidence — see "Staged escalation" |
| A5 | Auto-created statistics on the target cannot be prevented; schedule around statistics maintenance | Per-target run window; footprint reported; our own hashes must not be fooled by statistics churn |
| A6 | Targets are live production databases and replicas | Budgets in pages, MAXDOP, enforced timeouts, one session per target |
| A7 | The reasoning model differs per customer (local or frontier) | No reliance on thinking mode, context-size limits, estimates in calls/tokens/pages rather than minutes; model-derived score weight kept low |
| A8 | View columns that just pass through a base column inherit its meaning; **computed** view columns carry business logic and must be reasoned | View lineage in Phase 3; module definitions captured now rather than in the backlog |
| A9 | Before 3.0: create a PostgreSQL target with test data and replicate the SQL Server dialect for it | Sub-phase **3.P** (done, below). Every later deliverable is built for both dialects |
| A10 | LLM calls during development may use the assistant's own model "if constrained and allowed" | Not possible: the assistant cannot be called as an API from Gaia's environment, and its answers pasted in by hand would not be a measurement of the configured model. LLM-dependent runs use the configured gateway and are run by the owner; the assistant builds, tests with fakes, and reviews the exported results |

### What changed from the previous draft, and why

| Previous draft | Revised | Reason |
|---|---|---|
| Profile a column when score < 0.60, then re-reason | Cheap bounded profile **before** the first reasoning; targeted tests afterwards | With no comments and weak names the first, catalog-only LLM call yields almost nothing, so it is a guaranteed extra call per table. Profiling was measured at seconds per table; reasoning at minutes |
| `profile` earns 0.25 of evidence coverage when a column was profiled at all | Evidence ledger: each check **supports or contradicts** a claim | The old rule raised every profiled score by +0.075 regardless of content — a contradicted column scored *higher* when profiled (0.390 vs 0.315), and a plain column could never exceed 0.775, i.e. never be accepted (verified by running `score_column`) |
| Acceptance: "low-confidence columns show a measurable score increase" | Acceptance: **accuracy** against ground truth improves, and the score ranks correct above incorrect | The old criterion was satisfied by construction |
| `TABLESAMPLE SYSTEM/BERNOULLI`, 200 rows | `TABLESAMPLE SYSTEM` only (T-SQL has no `BERNOULLI`), sized in rows across many pages; top-k from exact `GROUP BY` | Page sampling is clustered; 200 rows is ~2 pages, the same bias as `TOP` |
| "No `TOP N` query exists", enforced by test | No `TOP` **used as a sample**; `TOP (k) … GROUP BY … ORDER BY count` is legal | The old test would reject the top-k query in OUTLINE §11 |
| Exact-first everywhere | Exact where cheap (small tables, columnstore, targeted tests); sampled/approximate on large rowstore, always labelled | The "exact is affordable" measurement used a unique key; production tables are larger and cold |
| Views not handled; `sys.sql_modules` in backlog | Module definitions captured; view column lineage; computed columns reasoned | Owner answer A8; aliases in views are among the strongest evidence when names are meaningless |
| Redis cache + distributed locks | PostgreSQL is the profile store; PG advisory locks | One process, no worker yet; a second copy of sample data buys nothing |
| Profiles invalidated by `data_hash` | Freshness policy: structure change, row-count delta, or age | `data_hash` includes `MAX(stats last_updated)`, which Gaia's own queries (auto-created stats) and nightly maintenance move |
| PII stored on `column_semantic` | Detectors produce a deterministic `semantic_type` on the profile; PII is one family of types | Lifecycle follows the data, not the reasoning version |

### Staged escalation

```text
Stage 0  metadata evidence      no data reads, no LLM      every object
Stage 1  light profile          bounded page reads         tables without catalog-sufficient evidence
Stage 2  reasoning + critique   1 LLM pass, all evidence   every object (views after their bases)
Stage 3  targeted verification  tests derived from claims  only unresolved claims; ≤ 1 round per run
         → re-reason only tables whose evidence changed
Stage 4  review queue           ranked for the SME         whatever is still below accept_at
```

**Stage 0 — metadata evidence (free).** Catalog (Phase 1); module definitions
from `sys.sql_modules` for views, procedures, functions and triggers; view
column lineage; statistics histograms (`sys.dm_db_stats_histogram`) as
full-table distributions for leading statistic columns; index usage
(`sys.dm_db_index_usage_stats`) as an importance signal only.

*Catalog-sufficiency pre-check (keeps the intent of A4).* A table skips Stage 1
when every column already carries strong catalog evidence (comment, key, trusted
FK, or check constraint). This is decided deterministically, without an LLM call.
On comment-less databases it rarely fires; on well-documented ones it saves the
target reads the original design wanted to save.

**Stage 1 — light profile (bounded).** Per table, by size class from the catalog:

| Class | Rule | Method recorded |
|---|---|---|
| small (≤ `full_scan_max_pages`) | one batched exact scan + exact `GROUP BY` top-k | `exact` |
| columnstore | batched aggregates (measured < 1 s on 24.6 M rows) | `exact` |
| large rowstore | `TABLESAMPLE SYSTEM (n ROWS) REPEATABLE(seed)` sized to `sample_target_rows`, capped by `max_sample_pages`; histograms where they exist | `sampled_pages` / `histogram` |

Per column: null fraction, distinct count/ratio, min/max (`bit` cast to `int`),
text length statistics, numeric mean/stddev on a `float` cast (cast recorded),
temporal granularity, parse rates (numeric-like / date-like text, `yyyymmdd`
integers), top-k, **empty/constant** flag.

On the sample, in Python, no further target reads:

* **Semantic-type detectors** with validators, not just regexes: email, URL,
  phone, IBAN (mod-97), card (Luhn), national ID (TCKN checksum), GUID, IPv4/6,
  ISO-4217 currency, ISO-3166 country, dates in text/int, JSON/XML in text,
  boolean-like text. Each result carries a match rate.
* **Cross-column identities** among a table's numeric columns (`a×b=c`,
  `a+b=c`, `a−b=c` within tolerance; bounded to `max_identity_columns`) and
  temporal order (`a ≤ b` in ≥ 99 % of rows). A verified `quantity × unit_price
  = line_total` identifies three measures without a single meaningful name.
* **Join candidates** for key-like columns with no FK, from type compatibility,
  distinct ratio and range containment within a PK's range — names not required.

Sample rows are processed in memory and **never persisted**; only aggregates and
at most `max_values_per_column` example values are stored.

**Stage 2 — reasoning (one pass).** `reason_table.v2` and `critique_table.v2`
receive an evidence block per column (profile summary, detector results,
identities, lineage/aliases, module references). New citable refs: `prof:<metric>`,
`det:<type>`, `idn:<id>`, `mod:<module>`, `test:<id>`. The critique sees the same
evidence, so it can falsify data-based claims. Views are reasoned after their
base tables (dependency order), because a computed column's meaning depends on
its inputs.

**Stage 3 — targeted verification.** Tests are derived **deterministically**
from the claims (no LLM call to design them) and run within the page budget:

| Claim | Test |
|---|---|
| `reference` / inferred join hint / join candidate | inclusion rate of child values in the parent key; fan-out (1:1 vs 1:N) |
| trusted, enabled FK | no orphan test (guaranteed); fan-out only |
| `identifier` on a large table | exact uniqueness |
| `enumeration` domain from a sample | exact `GROUP BY` top-k |
| `measure` / `timestamp` from a sample | exact range, sign, parse rate |

Results become ledger items. Only tables whose **evidence hash** changed are
re-reasoned, and at most one Stage 3 round runs per invocation, so there is no
loop.

**Stage 4 — review queue.** `semantics.review_item` per data source: object,
reason (abstain / evidence exhausted / contradiction / critique disagrees),
priority = `(1 − score) × importance` (row count, FK degree, index usage,
references from modules), assignee (nullable), status. Empty or constant
columns are flagged `unused` and kept out of the queue. SME decisions, when the
UI exists, enter the ledger with full authority and are never overwritten (D16).

**Degraded mode (catalog-only).** If the login has no `SELECT`, or profiling is
disabled for a target, Stages 1 and 3 are skipped and recorded as such. This is
the original "light reasoning" path. Under the names-alone rule its results route
to review rather than being accepted — which is the honest outcome.

### Evaluation of the owner's *light → profiling → deep* sequence (A4)

| Aspect | Light → profile → deep | Proposed staged escalation |
|---|---|---|
| Worst case (no comments, weak names) | First LLM pass has nothing to reason from; almost every column escalates → 4 calls/table | 2 calls/table, plus 2 only where Stage 3 changes the evidence |
| Best case (documented DB) | Light pass may resolve columns early | Catalog-sufficiency pre-check skips Stage 1 without an LLM call |
| Target load | Deep profiling of every flagged column | Bounded light profile for all + exact tests for few |
| What the "deep" step does | Generic deeper statistics | **Hypothesis tests** of specific claims — the evidence that actually moves a score |
| Where the original idea survives | — | Escalation principle, cost gating, catalog-only path as the degraded mode |

### Other evidence sources (A3: "if there are other methods")

| Source | What it tells | Cost | Where |
|---|---|---|---|
| View / procedure / function definitions | Aliases (`F_23 AS CustomerName`), expressions, join conditions in code | free | **Phase 3** (views, capture of all modules); alias mining from procedures → backlog |
| Statistics histograms | Full-table distribution of indexed/statted columns | free | **Phase 3** |
| Profile + semantic-type detectors | What kind of value a column holds | bounded reads | **Phase 3** |
| Cross-column identities, temporal order | Measures and lifecycle timestamps without names | free on sample | **Phase 3** |
| Inclusion dependencies | Undeclared FKs — join paths Phase 4 depends on | targeted reads | **Phase 3** |
| Query Store / plan cache | How applications join, filter (`WHERE x='A'` ⇒ code) and aggregate (`SUM(x)` ⇒ measure) | free; needs capability | backlog — high value |
| Known-product recognition | SAP, Dynamics, Logo, Netsis… have documented schemas the model may know | 1 call per DB | backlog |
| Cross-database transfer | SME labels from DB A as evidence for matching columns in DB B (same vendor schema) | free | backlog; Phase 3 stores a per-column profile signature to make it possible |
| Chat feedback | Failed or corrected queries point at wrong meanings | free | Phase 4+ |

### Views and computed columns (A8)

* View definitions are parsed with `sqlglot` (`read="tsql"`) and qualified
  against the catalog. Each view column is classified:
  * **pass-through** — a plain (possibly aliased) base column: linked to the base
    column, inherits its semantics, **not** reasoned or profiled separately. The
    alias is recorded as `mod:` evidence **for the base column**, because in a
    worst-case database a view alias is often the only business name anywhere.
  * **computed** — expression, aggregate or `CASE`: reasoned in the view bundle
    with the expression and the resolved base-column meanings as evidence.
    Profiled only if every base table is in the *small* class, or as a Stage 3
    test within budget; otherwise reasoned from the expression and recorded so.
  * **unresolved** — parse failure, encrypted module, dynamic SQL: treated as
    computed with the reason recorded.
* Table computed columns (`is_computed`, definition already captured) get the
  same expression-based treatment.
* `TABLESAMPLE` cannot be applied to views, so views are never sampled directly.
* Adding definitions to the snapshot changes the view hash payload, so
  `STRUCTURE_HASH_VERSION` is bumped **2 → 3**: one re-baseline pass, after which
  every current semantic row is stale. That is acceptable here because the v2
  prompts re-reason everything anyway.

### Scoring — `heuristic-v2` (A3)

Every piece of evidence becomes a ledger item: `{kind, ref, direction:
supports|contradicts, strength 0–1, source: catalog|module|profile|detector|
test|sme}`. Items are produced deterministically wherever possible; model
citations are accepted only if the ref exists and concerns the column.

```text
evidence_support   0.35   noisy-OR(supporting strengths) × (1 − noisy-OR(contradicting strengths))
coherence          0.20   mean of claim-vs-fact checks: catalog checks (Phase 2) + profile checks
counter_evidence   0.20   critique verdict (the critique now sees the evidence)
hypothesis_margin  0.15   as Phase 2
model_self_report  0.10   as Phase 2
```

Default strengths (config, placeholders until Phase 6 calibration): comment
0.60 · trusted FK 0.60 (untrusted 0.30) · key 0.50 · consistent check
constraint 0.50 · identity/computed definition 0.40 · view alias 0.40 ·
detector ≥ 95 % match 0.70 (80–95 % 0.40) · targeted test passed 0.70 · profile
consistency check passed 0.25 · **name: 0** (the name is where the hypothesis
comes from, not evidence for it). A failed profile check adds a 0.50
contradiction **and** a failing coherence check; a failed targeted test adds 0.80.

Profile consistency checks (claim vs data): `flag` ⇒ ≤ 3 distinct values;
`identifier` ⇒ distinct ratio ≥ 0.99; `code` ⇒ low cardinality and short
values; `measure` ⇒ numeric and not constant; `timestamp` ⇒ parses to plausible
dates; enumeration values ⇔ observed top-k; detector type ⇔ claimed role and unit.

Properties, computed with the defaults above (typical answer: confidence 0.85,
best alternative 0.30, critique *plausible* unless stated):

| Case | Score | Band |
|---|---|---|
| Names only | 0.522 | deep / review |
| Names only, perfect model and critique — **ceiling without evidence** | 0.650 | never accepted |
| Three profile checks pass | 0.724 | deep / review |
| Three checks pass, critique *supported* | 0.784 | deep / review |
| Detector 97 % + two checks, *supported* | 0.873 | accepted |
| Trusted FK + inclusion test passed, *supported* | 0.890 | accepted |
| One check **fails**, nothing passes | **0.455** | below the names-only baseline |
| One passes, one fails | 0.516 | ≈ baseline |
| Targeted test fails, critique *doubtful* | 0.385 | abstain |

So: names alone can never be accepted; generic consistency is not enough;
specific positive evidence is; contradiction lowers a score below the
no-evidence baseline. The full ledger is stored with every scored version — it is
the input Phase 6 needs to replace these placeholder strengths with a calibrator
fitted on SME labels.

**Coverage is separate from score.** Each column records the deepest stage
reached (`catalog`, `modules`, `light_profile`, `targeted`, `sme`). Routing uses
both: low score + stages left → escalate; low score + stages exhausted → review
queue. A low score is therefore never confused with "we did not look".

**`semantic_type`.** Detector- and profile-derived type (e.g. `email`,
`date_int_yyyymmdd`, `currency_amount`, `iso_country`) with its match rate,
stored on the profile. Deterministic and model-independent, and directly useful
to Phase 4 SQL generation even while the business meaning is still in review.

### Model independence and value exposure (A2, A7)

* **Thinking not required.** v2 prompts must validate with thinking off; the
  evaluation includes one thinking-off condition as a proxy for weaker models.
* **Context budget.** `llm.context_budget_tokens` (default 32k). Bundles whose
  estimated prompt exceeds it are split into column chunks; each chunk carries
  the compact table context (all column names/types, keys, FKs). Amends D17.
* **Estimates in calls, tokens and target pages.** Wall time is reported but is
  provider-specific.
* **Exposure mode** resolved from `llm.locality: local | remote`:
  * `local` → raw values.
  * `remote` → `masked` by default: detector-positive and free-text columns are
    sent as shapes and pattern classes with statistics; low-cardinality code
    values are sent raw. FPE is later.
  * `profiling.allow_raw_to_remote: true` is the explicit development override
    for synthetic data, and it logs a warning.
  * A test asserts that no literal sample value appears in a rendered prompt in
    masked mode.

### Target safety and footprint (A5, A6)

* **Statement timeout actually applied** on every target session via the ODBC
  query timeout (the Phase 1 reader claims one but never sets it). Profiling has
  its own `profiling.statement_timeout_s` (default 60), separate from the chat
  path's 30 s.
* `OPTION (MAXDOP n)` on every profiling query (default 2); on PostgreSQL the
  equivalent is `SET max_parallel_workers_per_gather = n` for the session.
  PostgreSQL sessions already enforce `statement_timeout`, `lock_timeout` and a
  read-only transaction (3.P).
* Budgets in **pages** per table and per run; `max_runtime_per_table_s` stays as
  a ceiling (180 s).
* One profiling session per target; PostgreSQL advisory lock per
  (data source, table).
* **Run window** per target (`profiling.window`, e.g. `22:00–06:00` target time),
  chosen after the statistics maintenance job (A5). The CLI refuses outside the
  window unless `--ignore-window`.
* **Footprint report.** `sys.stats` is diffed before and after each run; auto-created
  statistics attributable to Gaia are listed for the DBA. They are a mostly
  one-time effect per column. Amends D4: *Gaia issues only `SELECT`; the engine
  may create statistics as a side effect, which is reported, not prevented.*
  PostgreSQL has no such side effect: statistics come only from `ANALYZE`, so a
  profiling run changes nothing but `pg_stat` counters.
* **Profile freshness policy** replaces `data_hash` invalidation: re-profile on
  a structure change, a row-count move beyond `row_count_delta_ratio`, or age
  beyond `profile_max_age_days` (30). `data_hash` remains informational.
* Aggregates under `READ UNCOMMITTED` record the isolation level; on a busy table
  "exact" means "exact at read-uncommitted".

### Sampling rules (corrected)

1. T-SQL supports only `TABLESAMPLE [SYSTEM]`, which samples **pages**. There is
   no `BERNOULLI`. PostgreSQL has both `SYSTEM` (pages, same clustering caveat)
   and `BERNOULLI` (row-level, but reads every page); `tsm_system_rows` is an
   extension and cannot be assumed. Both engines sample tables and materialized
   views, never plain views.
2. Page samples are clustered (rows on a page are neighbours in the clustered
   key), so samples are sized in rows across many pages (`sample_target_rows`,
   default 10,000), never 200 rows. Sample-derived metrics are labelled
   `sampled_pages`.
3. Top-k and value distributions come from exact `GROUP BY` on small tables and
   in Stage 3; from the sample only in Stage 1 on large tables, labelled.
4. `TOP` is legal only as `TOP (k) … GROUP BY … ORDER BY count`, or as a row cap
   over a `TABLESAMPLE` source. An unordered `TOP` used as a sample is forbidden —
   enforced by a test that parses **every** generated profiler statement with
   `sqlglot`.
5. `REPEATABLE(seed)` reproduces a sample only while data and physical layout
   are unchanged; the seed is stored anyway.
6. `TABLESAMPLE` on clustered columnstore is to be verified in 3.0; the fallback
   is batched aggregates, which are cheap on columnstore.

### Deliverables

**3.P — PostgreSQL development target and dialect (A9) · DONE**
- `infra/pg-target/schema.sql`, `readonly_role.sql`, `seed.py` — synthetic worst-case
  retail database, deterministic generator, read-only role provisioning
- `docker-compose.yml` service `gaia-target-pg` (profile `target-pg`, `127.0.0.1:5434`);
  `make pg-target-up | pg-target-seed | pg-target-psql | pg-target-down | pg-target-reset`;
  `TARGET=<name>` on every target-facing make target
- `dialects/postgres/connection.py` (hardened session), `catalog.py` (MetadataReader),
  `probe.py` (read-only proof); `dialects/mssql/probe.py` (moved, unchanged)
- `dialects/types.py` — per-dialect type vocabularies; scorer type checks use them
- Lazy dialect registration (no driver import until a dialect is used); probe registry
- Config: `${VAR:-default}`, unresolved placeholders scoped per target, `options.sslmode`,
  target `demo_pg`; `ColumnSnapshot.type_definition` (enum/domain)
- Tests: `test_dialect_types.py` (SQL Server oracle), `test_pg_catalog_helpers.py`,
  `test_dialect_bootstrap.py`, `test_snapshot_compat.py` (golden Phase 2 hashes),
  config scoping tests, `integration/test_pg_target_live.py`

**3.0 — preconditions**
- Statement/query timeout applied in `dialects/mssql` sessions, with a test
  (`WAITFOR DELAY` beyond the timeout → recorded failure)
- **SQL Server probe hardening** (finding from 3.P): prove read-only from
  permissions (`fn_my_permissions` / `HAS_PERMS_BY_NAME`: no INSERT/UPDATE/DELETE/
  ALTER/CREATE anywhere), and aim the DDL attempt at a schema where the login could
  create, not only `dbo` — as the PostgreSQL probe now does
- Run `make test` with `GAIA_TEST_PG_TARGET_URL` on the real host (PostgreSQL 18)
- The missing Phase 2 test: one column edit re-reasons exactly one bundle
- Re-measurement: `dw.sales_wide` (widest large table), one non-key
  high-cardinality column, percentiles, cold cache (`DBCC DROPCLEANBUFFERS` is
  not available to `gaia_ro` — run as the owner, or measure after a restart)
- Verify auto-created statistics on the target (`sys.stats` before/after one
  `GROUP BY` query) and `TABLESAMPLE` on columnstore

**3A — evidence collection** (both dialects)
- `dialects/mssql/profiler.py`, `dialects/mssql/modules.py` (`sys.sql_modules`);
  `dialects/postgres/profiler.py`, `dialects/postgres/modules.py` (`pg_get_viewdef`,
  materialized views, `pg_proc` bodies); lineage parsed with `sqlglot`
  (`read="tsql"` / `read="postgres"`)
- `profiling/budget.py` (pages, window, timeouts, advisory locks), `profiling/sampler.py`,
  `profiling/statistics.py`, `profiling/detectors.py`, `profiling/identities.py`,
  `profiling/joins.py` (candidates + inclusion tests), `profiling/lineage.py` (views)
- `profiling/service.py` — stage orchestration, catalog-sufficiency pre-check,
  freshness policy, footprint report
- `db/models/profiling.py` — `profile_run`, `table_profile`, `column_profile`
  (metrics with per-metric `method`, `rows_scanned`, `pages`, `runtime_ms`, `seed`,
  `isolation`), `verification_test`, `view_column_lineage`, `module_definition`
- `alembic/versions/0004_profiling.py`; `STRUCTURE_HASH_VERSION` 2 → 3
- `tools/profile.py` — `--target --schema --table --stage --dry-run --ignore-window --report`

**3B — reasoning with evidence**
- `semantics/evidence.py` — ledger construction, evidence refs, evidence hash,
  exposure rendering
- `semantics/scoring.py` → `heuristic-v2` (the v1 weights remain loadable for
  comparison)
- `semantics/prompts/reason_table.v2.md`, `critique_table.v2.md`
- `semantics/service.py` — evidence hash in the skip key, chunking, dependency
  order for views, Stage 3 re-reason, coverage level, `semantic_type`
- `semantics.review_item`, `semantics.evidence_item` (in `0004` or `0005`)

**3C — evaluation**
- `tools/evaluate_semantics.py` — name-blind mode, conditions, judge, report
- A stratified set of 40 columns labelled by the owner (rehearses the SME step)

### Acceptance criteria

**3.P** — verified in a sandbox (PostgreSQL 16); see "3.P results"
- [x] Seed is deterministic: two full runs give identical table checksums
- [x] `check-target TARGET=demo_pg`: 9/9 PASS for `gaia_ro`; FAILs for a superuser and
      for a role that is writable only in one schema
- [x] Introspection: 28 objects / 213 columns; re-run `unchanged=28`, 0 writes;
      two reads byte-identical; a comment-only change is detected on exactly one
      table and reverting restores the hash
- [x] Every mapping decision asserted by a live test (identity/serial, generated,
      domain/enum, `NOT VALID`, disabled FK, partitions, expression/partial/INCLUDE
      indexes, materialized view, comments)
- [x] SQL Server unaffected: sample-snapshot structure hash and rendered bundles
      byte-identical to Phase 2 (golden test); scorer type fit identical for every
      role × type combination (oracle test); prompt versions unchanged
- [x] Reasoning dry run on `demo_pg` selects FK neighbours correctly and renders bundles
- [ ] Same on the real host with `postgres:18` (owner)

**3.0**
- [ ] A target statement exceeding the timeout is cancelled and recorded
- [ ] The SQL Server probe fails for a login that can write in any schema
- [ ] The single-column re-reasoning test exists and passes
- [ ] Re-measurement table added below, including `dw.sales_wide` and cold cache
- [ ] Auto-statistics and columnstore-sampling behaviour verified and recorded

**3A**
- [ ] Every in-scope table has a profile or a recorded skip reason
      (`catalog_sufficient`, `no_select`, `budget`, `window`, `view`)
- [ ] Every metric records `method`, `rows_scanned`, `pages`, `runtime_ms`,
      `isolation`, and `seed` where sampled
- [ ] No generated profiler statement contains an unordered `TOP` used as a
      sample (sqlglot-based test over all statements)
- [ ] No table exceeds its page budget or the 180 s ceiling; degradation is recorded
      per metric
- [ ] Detectors pass positive/negative fixtures, including checksum validators
- [ ] Every view column is classified pass-through / computed / unresolved;
      the three `rpt` views are checked by hand
- [ ] Every inferred join hint has an inclusion-test result
- [ ] A second run inside the freshness window issues **zero** data queries
- [ ] The footprint report lists auto-created statistics attributable to the run
- [ ] With `SELECT` denied (simulated), the run completes in catalog-only mode

**3B**
- [ ] Scoring unit tests prove: a contradiction lowers the score below the
      no-evidence baseline; being profiled without passing checks changes
      nothing; names alone cannot reach `accept_at`; specific evidence can;
      breakdown still sums exactly to `raw_score`
- [ ] The ledger is stored for every scored version
- [ ] Re-running with unchanged evidence issues **zero** LLM calls; changing one
      table's evidence re-reasons exactly that table
- [ ] A bundle over `context_budget_tokens` is chunked and validates
- [ ] In `masked` mode no literal sample value appears in any rendered prompt
- [ ] Every column below `accept_at` with exhausted coverage has a review item;
      `unused` columns are flagged and excluded

**3C** (see "Evaluation design")
- [ ] Name-blind, profiled accuracy beats name-blind, catalog-only by at least the
      agreed margin (P3-4)
- [ ] Named, profiled accuracy ≥ named, catalog-only (Phase 2 baseline)
- [ ] The score is informative: AUROC (score vs correct) ≥ 0.70 in the name-blind,
      profiled condition
- [ ] Owner audit of a random 20 % of judge decisions agrees ≥ 90 %
- [ ] Full run over all objects in the production mode (named, profiled): calls,
      tokens, target pages and wall time reported per table

### Evaluation design — simulating the worst case

Ground truth is the scarce resource, and the premise says there usually is none.
`nge_small` has descriptive names, which makes it usable as a **worst-case
simulator**:

* **Name-blind mode.** The bundle builder replaces every table, column,
  constraint, index and module identifier with opaque tokens (`T017.C04`) and
  rewrites check, computed and view definitions through `sqlglot`. Data values are
  untouched. The hidden original names and types become the reference meaning.
* **Conditions**, on the Phase 2 ten-table scope first:

  | Code | Names | Evidence | Notes |
  |---|---|---|---|
  | N-C | visible | catalog only | = Phase 2 results, already stored |
  | N-P | visible | full Phase 3 | production mode |
  | B-C | hidden | catalog only | floor |
  | B-P | hidden | full Phase 3 | **the worst case the product is built for** |
  | B-P-nt | hidden | full Phase 3, thinking off | proxy for a weaker / local model |
  | N-P-m | visible | full Phase 3, masked exposure | cost of redaction for remote models (optional) |

* **Metric.** Role accuracy (exact match) and meaning match ("same business
  meaning? yes / partial / no") judged by an LLM against the reference. The
  owner audits a random 20 % of judgements, and labels a stratified 40-column
  set for the named conditions, where the original names are not a fair
  reference. Score informativeness is measured as AUROC.
* **Caveat.** Original names are a proxy, not truth, and the judge shares a
  model family with the reasoner. The audit exists to catch both.
* **A native worst case on `demo_pg` (3.P).** `legacy.tb_trx02` and `tb_mst01` have
  meaningless names by construction, and `rpt.v_legacy_transactions` carries the
  true names as aliases. With the view hidden from the reasoner, those columns
  have an exact ground truth that needs neither obfuscation nor a judge; with
  the view visible, they measure alias back-propagation (A8).

### Decisions needed

Phase 3 decisions use a `P3-` prefix to end the numbering collisions between
phases (12, 13, 15 and 16 were each used twice).

| # | Decision | My recommendation |
|---|---|---|
| P3-1 | Staged escalation (light profile before the first reasoning; catalog-only as degraded mode) instead of reason → profile → re-reason | **Adopt** |
| P3-2 | Scoring `heuristic-v2` (ledger, support/contradiction, names alone never accept, coverage separate from score) | **Adopt**; strengths are placeholders for Phase 6 |
| P3-3 | Deterministic `semantic_type` on the profile | **Adopt** |
| P3-4 | Evaluation by name-blind simulation; margin for B-P over B-C | **Adopt**; margin **+15 points** of meaning accuracy; owner labels 40 columns (~1 h) |
| P3-5 | Stage 1 defaults | `full_scan_max_pages` 12,800 (~100 MB) · `sample_target_rows` 10,000 · `max_sample_pages` 2,000 · MAXDOP 2 · profiling timeout 60 s · 180 s per-table ceiling · no window in development |
| P3-6 | Profile freshness policy replaces `data_hash` invalidation | **Adopt**: 20 % row delta, 30 days |
| P3-7 | Exposure modes (`local` raw, `remote` masked, explicit dev override) | **Adopt**; run N-P-m once to price redaction |
| P3-8 | Views per A8; capture all module definitions; procedure alias mining to backlog | **Adopt** |
| P3-9 | Chunk bundles over `llm.context_budget_tokens` (amends D17) | **Adopt**, 32k default |
| P3-10 | No Redis in Phase 3 (PG store + advisory locks) | **Adopt** |
| P3-11 | Include the thinking-off condition B-P-nt | **Adopt** |
| P3-12 | `accept_at` stays 0.80 under `heuristic-v2` until Phase 6 calibrates | **Adopt** |
| P3-13 | Sub-phase gates (3.0 → 3A → 3B → 3C), each approved separately | **Adopt** — Phase 2's estimate was 7× off; smaller gates bound the damage |

**Taken 2026-11-14:** all thirteen adopted as recommended (owner: "Approved").
The owner will label the 40-column set (P3-4). D12 precedence amended as
proposed (D12′).

### Estimate

Effort is a rough range; the Phase 2 estimate was 7× off, so each sub-phase
re-estimates at its gate.

| Sub-phase | Size | Rough effort |
|---|---|---|
| 3.0 | S | 1–2 days |
| 3A | L | 5–7 days |
| 3B | L | 4–6 days |
| 3C | M | 2–3 days |

LLM volume, assuming ~5k tokens per call (Phase 2 averaged ~2.3k; the evidence
block roughly doubles it):

| Run | Calls | Tokens |
|---|---|---|
| Experiments: 4–5 conditions × 10 tables × ~2.6 calls + judge | ~180 | ~1 M |
| Full production-mode run, 71 objects × ~2.6 calls | ~185 | ~1 M |

This is well inside the gateway's 15 M/day, but spans windows of 2.5 M. At Phase 2's
observed throughput (~20–30 calls/hour with thinking), that is **12–18 h of wall
time**, so runs go overnight. This is the dominant schedule risk, not profiling.

Target I/O is bounded by page budgets and reported per run.

### Risks

| Risk | Mitigation |
|---|---|
| Placeholder strengths make the score look precise when it is not | Ledger stored; AUROC measured; Phase 6 calibrates on SME labels |
| Judge bias (same model family) | 20 % owner audit with an agreement threshold |
| Name-blind proxy is imperfect | Owner-labelled 40-column set for named conditions |
| `sqlglot` fails on complex T-SQL views | `unresolved` class with reason; counted in the report |
| Detectors are locale-specific | Pluggable registry; TR + international set first |
| Auto-created statistics upset a DBA | Footprint report, run window, documented in onboarding (D4 amendment) |
| LLM wall time | Overnight runs, subset first, thinking-off condition measured |
| Page samples biased by clustering | Row-sized samples across pages; histograms; Stage 3 exact confirmation |

### Measured profiling cost (2026-11-13, `nge_small`) — kept, with corrections

| Operation | `dw.fact_sales` 24.6M (has NCCI) | `dbo.transaction_lines` 24.6M (rowstore) |
|---|---|---|
| `COUNT_BIG(*)` | 543 ms | 1,299 ms |
| `MIN/MAX/COUNT` one column | 33 ms | 5,910 ms |
| batched `MIN/MAX/COUNT`, 7 columns / 18 aggregates | **368 ms** | one scan, ~6 s |
| `COUNT(DISTINCT)` low-cardinality (4 values) | 174 ms | 917 ms |
| `COUNT(DISTINCT)` high-cardinality (24.6M values) | **11 ms** | 438 ms |
| `APPROX_COUNT_DISTINCT` same column | 2,388 ms | 795 ms |
| `AVG` + `STDEV` on decimal | **7,395 ms** | — |
| `AVG` with `CAST(decimal AS float)` | 1,082 ms | — |
| `TABLESAMPLE SYSTEM (1 PERCENT)` | 186 ms | 51 ms |

Findings that still stand: **`TOP (N)` is not a sample** (the clustered identity
key is in arrival order); **`MIN`/`MAX` fail on `bit`**; **columnstore segment
metadata is not usable** for profiling (encoded ids, wrong `has_nulls`);
statistics histograms are a usable tier-0 seed.

Corrections from the 2026-11-14 review:

1. The high-cardinality `COUNT(DISTINCT)` was measured on `transaction_line_id`,
   a **unique key**; the optimizer most likely skipped the distinct work. A
   non-key high-cardinality column must be measured in 3.0 before "exact is
   cheap" is generalised.
2. The `STDEV` speed-up compared *AVG + STDEV on decimal* with *AVG only on
   float*. The float cast is still the plan, but its benefit for `STDEV` is
   unproven until re-measured.
3. `APPROX_COUNT_DISTINCT`'s 2.3 % error is roughly in line with its documented
   accuracy and irrelevant to meaning (the distinct *ratio* is what matters).
   The choice between exact and approximate is made on cost, and the method is
   always recorded.
4. Timings are from a dedicated dev host with a 6.9 GB database in a 31 GB
   machine, i.e. likely warm cache; production budgets are therefore expressed in
   pages.
5. `dw.sales_wide` (the widest large table) and percentiles were not measured.
   `PERCENTILE_CONT` sorts the full table; SQL Server 2025's
   `APPROX_PERCENTILE_CONT` or the sample is the default.
6. `TABLESAMPLE BERNOULLI` does not exist in T-SQL.

### Known input from Phase 2

Ten tables / 99 columns are reasoned (catalog only, names visible): 65
`deep_profile`, 32 `deep_profile_flagged`, 2 `abstain`, 0 accepted. They become
condition N-C. The other 61 objects have never been reasoned; under P3-1 they
are reasoned once, with evidence, rather than twice.

### 3.P results (2026-11-14, sandbox)

Environment: target = PostgreSQL 16.15 on `127.0.0.1:5434`; metadata store =
PostgreSQL 16 with pgvector 0.6.2 and all five contrib extensions on `:5433`,
migrations `0001`–`0003` applied (this also executed the `0003` trigram indexes
in a sandbox for the first time). No Docker there, so the Compose service is
verified on the real host.

| Check | Result |
|---|---|
| Seed, scale 1 | 2,973,244 rows in 96 s; two runs → identical checksums over `orders`, `order_lines`, `tb_trx02`, `customers` |
| Fixture properties | `total = subtotal + tax + shipping` and `subtotal = Σ line_subtotal` hold on every order; `f7 = f5 × f6` on every legacy row; 0 temporal-order violations; 858/858 national IDs and 641/641 IBANs checksum-valid; legacy `f2` → customers inclusion 98.9 % |
| Deliberate imperfections | `NOT VALID` FK with orphans; `NOT VALID` check with 3 violating rows; both FKs of `finance.invoices` with enforcement triggers disabled, 20 orphan invoices |
| `check-target TARGET=demo_pg` | 9/9 PASS for `gaia_ro` (DDL denied with SQLSTATE 42501) |
| Probe negative tests | superuser → 2 FAIL with every reason listed; a role writable only in `legacy` → FAIL (after the fix below) |
| `introspect TARGET=demo_pg` | 28 objects (24 tables, 4 views), 213 columns; re-run `unchanged=28`, 0 rows written |
| After a reseed | `structure_changed=0`, `data_changed=25`, 0 columns written |
| Determinism | two reads → byte-identical 105 KB document |
| Comment-only change | `structure_changed` on exactly `legacy.tb_trx02`; `data_hash` unchanged; revert restores the hash |
| Reasoning dry run | `sales.orders --with-neighbours` → 6 tables, exactly the FK neighbours; prompt versions unchanged (`reason_table.v1+2541a66663`) |
| SQL Server compatibility | sample snapshot hash and rendered bundles identical to Phase 2 (golden); type fit identical on 42 types × 8 roles × 7 names × key/FK flags (oracle) |
| Tests | 211 passed, 2 skipped (Ollama live) |

Findings, each fixed and covered by a test:

1. **A DDL probe in one safe schema proves nothing.** A role with `CREATE` on
   `legacy` and `INSERT` on one table passed the live DDL attempt in `public`.
   Read-only is now proven from privileges, and the live attempt aims at a schema
   where `CREATE` is granted if one exists. The SQL Server probe has the same blind
   spot (it only tries `dbo`) → 3.0.
2. **`default_transaction_read_only` is not a boundary.** `SET TRANSACTION READ
   WRITE` overrode it in the negative test. It stays as a seatbelt; the probe treats
   a read-only-transaction error as *inconclusive*.
3. **The scorer's type vocabulary was SQL Server's.** PostgreSQL `timestamp` is a
   date-time, SQL Server `timestamp` a row version; `boolean` was not a flag type.
   Per-dialect vocabularies now, with SQL Server proven unchanged.
4. **One unresolved placeholder anywhere failed every `check-target`.** Adding a
   second target without its password would have broken SQL Server checks.
   Placeholders are scoped per target; `${VAR:-default}` added for non-secrets.
5. **Every install needed an ODBC driver** because the bootstrap imported
   `pyodbc` eagerly. Registration is lazy now.
6. **Catalog text depends on `search_path`.** `format_type` and `pg_get_expr`
   qualify names relative to it, so structure hashes would differ by role
   configuration. Sessions pin `search_path = pg_catalog`.
7. Two query-level defects: the planner evaluated `has_sequence_privilege` before
   the `relkind` filter (guarded with `CASE`); psycopg parsed `%` in `LIKE`
   patterns as placeholders.
8. Pre-existing: an unreachable line after `return` in the SQL Server reader
   referenced an undefined name (lint F821); removed.

Known limits, deliberately deferred:

* Materialized views are reported as `view` (the downstream kind vocabulary is
  `table | view`); 3A's module capture records the difference.
* Foreign tables are skipped (profiling them would query remote servers).
* `type_definition` lives in the snapshot document (JSONB), not in
  `catalog.columns`; `0004` may add a column.
* PostgreSQL 18 specifics (NOT NULL rows in `pg_constraint`, virtual generated
  columns) are handled in code but untested until the host run.
* Row counts are `reltuples` estimates, like SQL Server's `sys.partitions`
  fallback; exact counts come from profiling.

### Roadmap impact — accepted 2026-11-14 and applied

R1 → Phase 5 rewritten; R2 → Phase 4 retrieval rule; R3 → Phase 6; R4 → migration
references; R5 → decisions log; R6 → Phase 7 backlog.

| # | Proposal | Why |
|---|---|---|
| R1 | Move **SME review (API + minimal UI)** out of the Phase 7 backlog into the main line — e.g. Phase 5 becomes "Web UI: SME review first, then chat" | A1: human-in-the-loop is the most important part; Phase 3 produces the queue and the feedback contract it needs |
| R2 | Phase 4 retrieval uses every non-abstain meaning, ranked by score, with uncertainty disclosed — not "accepted only" | In the worst case almost nothing is accepted before an SME has looked |
| R3 | Phase 6 calibrates from SME labels per model (later per data source), using the Phase 3 ledger | A7: model confidence differs per provider |
| R4 | Migration numbers are assigned at creation; Phase 3 takes `0004` | `0004` was planned twice (profiling and credentials) |
| R5 | Decision log amendments: D4 (statistics footprint), D14 (Phase 3 inputs), D17 (chunking); D12 precedence becomes **config > decisions log > PHASES > OUTLINE** | OUTLINE §7 is stale (`localhost`, concurrency 4, 120 s) and would otherwise override newer decisions |
| R6 | Backlog additions: Query Store evidence, procedure alias mining, known-product recognition, cross-database label transfer, data-at-rest handling of samples | Evidence sources identified above; A2 deferral recorded |

### Gate

- [x] **Approved to start Phase 3** · [x] decisions P3-1 … P3-13 answered · [x] roadmap proposals R1–R6 accepted (2026-11-14)
- [ ] 3.P verified on the real host (`make pg-target-up pg-target-seed`, `make check-target TARGET=demo_pg`, `make test` with `GAIA_TEST_PG_TARGET_URL`) → approved to start 3.0
- [ ] 3.0 complete → approved to start 3A
- [ ] 3A complete → approved to start 3B
- [ ] 3B complete → approved to start 3C
- [ ] 3C complete → **Phase 3 done**

---

## Phase 4 — Retrieval, SQL generation, guarded execution

**Goal.** Turn a business question into correct, safe SQL over the configured
targets — SQL Server (`nge_small`) and PostgreSQL (`demo_pg`, if P4-7 is
adopted) — and return an answer.

### Deliverables

- `query/retrieval.py` — hybrid: pgvector similarity + trigram + FK expansion.
  **Uses every non-abstain meaning, ranked by score, and discloses uncertainty
  (R2)**; `accepted` is not a filter, because in the worst case almost nothing is
  accepted before an SME has looked. `semantic_type` (Phase 3) informs generation
  (e.g. `date_int_yyyymmdd` needs a conversion)
- `query/planner.py` — entities, measures, filters, grain, joins; clarification
  decision when ambiguous
- `query/generator.py` — SQL generation in the target's dialect with compact schema context
- `query/validator.py` + `dialects/<d>/guard.py` — `SqlGuard` per dialect on a
  `sqlglot` AST (`read="tsql"` / `read="postgres"`)
- `query/executor.py` — read-only execution, timeouts, row caps
- `query/answerer.py` — natural-language answer from results
- `api/chat.py` — SSE streaming endpoint
- `db/models/chat.py`, chat migration (number assigned at creation, R4)
- `tools/ask.py` CLI
- **Data source management** (see OUTLINE §24): encrypted credentials on
  `catalog.data_sources` (next free migration number — `0004` is taken by Phase 3, see R4), a `TargetResolver` that reads targets from the
  database, the onboarding probe reused from `dialects/<d>/probe.py` (3.P), per-target
  limits, and an audit table for connection changes

### Validation rules (non-negotiable)

Single statement; `SELECT`/`WITH` only; identifiers resolved against the catalog;
join paths must exist as FK edges or confirmed hints; reject `xp_cmdshell`,
`OPENROWSET`, `OPENDATASOURCE`, `OPENQUERY`, `BULK INSERT`, `EXEC`, four-part
names, temp-table writes; inject `TOP (n)` when no limit exists.

PostgreSQL additionally: reject `COPY`, `DO`, `SET`/`RESET`, `CALL`, `LOCK`,
`dblink*`, `lo_*`, `pg_read_file`/`pg_ls_dir`-style file functions, and
state-changing functions (`nextval`, `setval`, `pg_advisory_*`,
`pg_terminate_backend`, …); inject `LIMIT n` when no limit exists; execute inside
a `READ ONLY` transaction.

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
Renumbered `P4-n` (formerly 11–16, which collided with Phase 3 and Phase 6).

| # | Decision | My recommendation |
|---|---|---|
| P4-1 | Golden question set: who authors ground truth | **you supply ~20 real business questions**; I write the expected SQL and you correct it. For `demo_pg` the generator's semantics are known, so its golden set can be written up front |
| P4-2 | Answer style: numbers only, or narrative + table | narrative + table + SQL |
| P4-3 | Default row limit | 1000 |
| P4-4 | Clarify vs best-guess on ambiguity | **clarify** |
| P4-5 | Admin-supplied connections: in Phase 4 or Phase 5 | **Phase 4** — the query path needs a target resolver anyway, and Phase 5's UI must not invent credentials storage |
| P4-6 | Credential encryption | **application-level AES-GCM**, master key in `.env`; `pgcrypto` is the fallback if key rotation outside the DB proves awkward |
| P4-7 | PostgreSQL in Phase 4 scope | **yes** — a second `SqlGuard` and generator dialect are small with `sqlglot`, and `demo_pg`'s `legacy` schema is the hardest retrieval case available |

### Gate

- [ ] **Approved to start Phase 4** · [ ] golden questions available

---

## Phase 5 — Web UI: SME review first, then chat (R1)

**Goal.** An SME assigned to a database reviews and corrects Gaia's meanings in a
browser, in priority order; then a user asks a question and sees the answer, the
SQL and the result.

Human-in-the-loop is the most important part of the product (premise A1), so
the review screens come first. Phase 3 supplies the ranked `review_item` queue,
the evidence ledger and the `sme` evidence kind this UI writes to.

### Deliverables

- Vite + React + TypeScript + pnpm; i18next with `en` locale; `api/client.ts`
- **Review (first):**
  - `api/review.py` — queue, item detail, decision endpoints
  - Screens: data sources → review queue per data source (priority, reason,
    filters) → item detail (meaning, alternatives, score breakdown, evidence
    ledger, lineage, profile summary within the exposure policy)
  - Actions: accept, correct (edit fields), reject, mark unused, add glossary term;
    assignment of a reviewer per data source
- **Chat (second):** SSE consumption; `MessageList`, `SqlViewer`, `ResultTable`,
  `ConfidenceBadge`; streaming, error states, clarification prompts

### Acceptance criteria

- [ ] An SME works through a data source's queue in priority order; each decision
      is stored as `sme_reviewed` / `sme_rejected` with reviewer and timestamp
- [ ] SME decisions enter the ledger as `sme` evidence and are never overwritten
      by re-reasoning (D16); a later structure change marks them `stale`, visibly
- [ ] Corrections propagate where lineage is certain (view pass-through columns)
      and are re-queued elsewhere rather than silently copied
- [ ] End-to-end question answered in the browser against each configured target
- [ ] Streaming renders progressively; no full-page reloads
- [ ] SQL is visible and copyable; result table scrolls and formats numbers/dates
- [ ] Confidence badge shows score **and** breakdown on hover/expand
- [ ] No hard-coded English strings outside locale files
- [ ] `pnpm build` succeeds; dev server on `127.0.0.1:5173`

### Decisions needed

| # | Decision | My recommendation |
|---|---|---|
| P5-1 | Identity for reviewers (decisions need a "who") | minimal local accounts with roles `admin` / `sme` / `user` first; OIDC later behind the same interface |
| P5-2 | Review granularity | column, with table-level grain/purpose reviewed on the same screen |

### Gate

- [ ] **Approved to start Phase 5**

---


## Phase 6 — Evaluation & calibration

**Goal.** Make quality measurable and regressions visible, and turn the
heuristic score into a calibrated probability.

### Deliverables

- `db/models/evaluation.py`, evaluation migration (number assigned at creation, R4)
- Golden question suite with expected SQL and expected result shape
- Column-purpose test set with SME-agreed labels (seeded by the owner's 40-column
  set from 3C, then grown from Phase 5 review decisions)
- Calibration (R3): fit isotonic/Platt on SME-reviewed examples **per reasoning
  model** (model confidence differs per provider, A7), later per data source;
  inputs are the Phase 3 evidence ledgers; store the calibrator version beside
  the score
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
Renumbered `P6-n` (formerly 15–16).

| # | Decision | My recommendation |
|---|---|---|
| P6-1 | Calibration needs reviewed examples — how many are acceptable | ~150 reviewed columns per model as a first fit |
| P6-2 | Which metric is the primary gate | **execution accuracy** on golden questions |

### Gate

- [ ] **Approved to start Phase 6**

---

## Phase 7 — Masking, refresh, scale-out (backlog)

Not scheduled. Listed so scope creep has somewhere to land.

- ~~SME review queue + UI~~ — moved into Phase 5 (R1); Phase 3 builds the queue
- PII masking applied to samples and answers, not just detected
  — *Phase 3 adds exposure modes for remote models (P3-7); FPE and data-at-rest of
  samples (metadata store, `audit.llm_call`) stay here*
- Scheduled incremental refresh (respecting per-target run windows, A5)
- Further dialects beyond SQL Server and PostgreSQL (MySQL, Oracle …) — the seam
  and the per-dialect type vocabulary exist since 3.P
- Multi-tenant access control and row-level-security awareness in cache keys
- ~~View/procedure definitions from `sys.sql_modules`~~ — moved to Phase 3 (capture of
  all modules, view lineage); **procedure alias mining** remains here
- Query Store / plan-cache evidence (how applications join, filter, aggregate)
- Known-product schema recognition (SAP, Dynamics, Logo, Netsis …)
- Cross-database transfer of SME labels (Phase 3 stores per-column profile signatures)

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
| D3 | Target | SQL Server 2025, `nge_small`, external container `sql1` (see also D25) |
| D4 | Target provisioning | Owner restores; Gaia never creates or alters the DB *(amended by D4′)* |
| D5 | Read-only login | Owner creates; Gaia verifies only |
| D6 | Metadata store | PostgreSQL 18 + pgvector, `127.0.0.1:5433` |
| D7 | Cache | Redis, ephemeral only, `127.0.0.1:6380` |
| D8 | Backend runtime | Host process in `gaia-env`, not containerised |
| D9 | Frontend | React + TypeScript + Vite + pnpm, i18next from the start |
| D10 | Catalog re-read | Always full; cheapness recovered via hashing |
| D11 | Target host format | Always `127.0.0.1`; never `localhost` (IPv6 resolution breaks ODBC) |
| D12 | Conflict rule | ~~Where documents disagree, `config/gaia.yaml` and OUTLINE.md win~~ *(superseded by D12′)* |
| D13 | Thinking | On for semantics and critique (config); per-run `--no-thinking` |
| D14 | Phase 2 inputs | Catalog facts only, English output, PII deferred to Phase 3 *(Phase 3 inputs: D14′)* |
| D15 | Providers | DeepSeek = reasoning (Claude/Gemini may be evaluated later); Ollama = embeddings only |
| D16 | `join_hint` / `glossary_term` | FK = confirmed, model = inferred, SME never overwritten; glossary for human-in-the-loop / company glossary as ground truth |
| D17 | Wide tables | ~~No chunking for now; token efficiency deferred to go-to-market tuning~~ *(superseded by D17′)* |
| D18 | Status bands | `auto_approved` ≥ `accept_at`, else `draft`; abstain band flagged for SME; tunable |
| D19 | LLM concurrency | **3**, one below the gateway's hard ceiling of 4 per model |
| D20 | Retry policy | Transient capacity (`429/502/503/504`) gets 8 attempts, 60 s cap, jitter, `Retry-After`; hard errors stay at 3 |
| D21 | Wide-bundle timeout | `llm.timeout_s` = **1800 s**; read timeouts retry once (`max_attempts_timeout: 2`); re-assess on provider change |
| D22 | `thinking` in skip key | **Deliberately excluded** — the default path always uses thinking; `--force` upgrades a table stored without it |

### Amendments and additions — in force since 2026-11-14 (Phase 3 approval)

| # | Amends | Value |
|---|---|---|
| D4′ | D4 | Gaia issues only `SELECT`; the engine may auto-create statistics as a side effect, which Gaia **reports** (footprint) but cannot prevent |
| D12′ | D12 | Precedence: `config/gaia.yaml` > decisions log > PHASES.md > OUTLINE.md (OUTLINE §7 is stale and must not override newer decisions) |
| D14′ | D14 | Phase 3 inputs: catalog + module definitions + histograms + bounded profile + targeted tests |
| D17′ | D17 | Bundles above `llm.context_budget_tokens` are chunked by column (local models have small contexts) |
| D23 | new | Design premise: worst case by default — no comments, no dictionary, possibly meaningless names; SME per database is the ground truth |
| D24 | new | Value exposure per provider: `local` raw, `remote` masked, explicit dev override |
| D25 | new | Second development target: PostgreSQL `demo_pg`, a synthetic worst-case database generated by `infra/pg-target/seed.py`, separate server on `127.0.0.1:5434` (Compose profile `target-pg`). The only database Gaia's tooling creates; it is a fixture, not a customer target |
| D26 | new | Dialects register lazily and carry their own type vocabulary (`gaia.dialects.types`); a change to dialect support must leave existing SQL Server hashes and prompts byte-identical (golden + oracle tests) |
| D27 | new | Config placeholders may carry non-secret defaults (`${VAR:-default}`); unresolved placeholders are scoped per target for `check-target` |
| D28 | new | LLM-dependent runs use the configured gateway and are executed by the owner; the assistant's own model is not a Gaia provider (A10) |

## Definition of done (per phase)

A phase is done only when all of the following hold:

1. Every acceptance criterion is ticked, and each tick corresponds to a command
   that was actually run — not to code that merely exists
2. Tests cover the new logic, and the full suite passes
3. `OUTLINE.md` records the verified results and any defects found
4. A short summary states what was built, what was proven, and what was deferred
5. Your gate is ticked

## Open items

Nothing blocks Phase 3. Next steps, in order:

1. **Owner:** verify 3.P on the real host — `make pg-target-up`, `make
   pg-target-seed`, `make check-target TARGET=demo_pg`, `make introspect
   TARGET=demo_pg`, and `make test` with `GAIA_TEST_PG_TARGET_URL` set — then tick
   the 3.P gate.
2. **Assistant:** 3.0 — SQL Server statement timeout, SQL Server probe hardening,
   the single-column re-reasoning test, re-measurements (the owner runs the
   cold-cache ones, which need `sa`).
3. **Owner, before 3C:** label the stratified 40-column set (P3-4).
4. **Later decisions:** P4-7 (PostgreSQL in Phase 4), P5-1/P5-2 (reviewer identity,
   review granularity).

Resolved, kept for the record:

| Was | Resolution |
|---|---|
| Phase 3 gate, P3-1 … P3-13, R1–R6 | Approved 2026-11-14; D12′ precedence accepted |
| Use the assistant's model for LLM calls | Not possible from Gaia's environment (A10, D28) |
| `RESTORE.md` | Dropped; the database is already restored (OUTLINE §4.1 keeps the commands) |
| Profiling budget: 120 s per table, 200-row samples, approximate above a threshold? | Replaced by page budgets, size classes and row-sized samples (P3-5); 180 s remains a ceiling |
| "Masking policy confirmed" gate | Replaced by exposure modes per provider (A2, P3-7) |
| Thinking mode on wide bundles | `llm.timeout_s` 600 → 1800; read timeouts have their own retry budget |
| Is `thinking` part of the skip key? | No, and it stays that way — the default path always uses thinking, so the gap is deliberate |
