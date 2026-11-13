# Gaia — Phase Plan (review & control)

> **Purpose.** A checkable plan for what gets built, in what order, and what
> must be true before each phase is considered finished. Use this to approve,
> reorder, or cut scope **before** work starts.
>
> This document is the control surface. `OUTLINE.md` is the design rationale;
> this file is the delivery contract.
>
> Last updated: 2026-11-13 · Status: Phases 0–2 complete and verified · Phase 3 awaiting approval

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
| 3 | Deep profiling, PII, re-reasoning | **planned** | ⬜ awaiting approval |
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
| Migrations | `0001` baseline, `0002` catalog, `0003` semantics — at head |
| Redis | `127.0.0.1:6380`, healthy |
| Target | SQL Server 2025 CU7, `nge_small`, login `gaia_ro` (read-only, proven) |
| Catalog captured | 71 objects, 607 columns, 93 FKs, 166 indexes, **0 comments** |
| Reasoning LLM | `deepseek-v4.1-flash` via gateway, JSON mode + thinking verified |
| Gateway limits | 4 concurrent requests per model, 1200 req + 2.5M tokens per window, 15M/day |
| Embeddings | Ollama `bge-m3`, 1024 dims, verified |
| Semantics captured | 10 tables, 99 columns, 109 embeddings, 94 join hints |
| Tests | 131 passing, 1 skipped |

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
- [ ] **No `TOP N` query exists in the profiler** — sampling is
      `TABLESAMPLE ... REPEATABLE(seed)`, enforced by test
- [ ] `bit` columns are cast to `int` before `MIN`/`MAX`; decimal aggregates run
      on a cast value and record the cast
- [ ] Every metric records whether it was exact, approximate or skipped, and why
- [ ] No profiling query exceeds its statement timeout; degradation is recorded
- [ ] The widest table completes within the 180 s ceiling
- [ ] Low-confidence columns show a measurable score increase after re-reasoning;
      report the before/after distribution
- [ ] PII classifications stored with evidence and confidence
- [ ] Profiling is idempotent per `data_hash`; unchanged data reuses cached profiles
- [ ] Total profiling wall time and rows scanned are reported

### Decisions needed

> Decisions 7, 12 and 13 are already answered by measurement — see
> **Measured profiling cost** below, which was run against `nge_small` first.

| # | Decision | My recommendation |
|---|---|---|
| 7 | Profiling budget per table | **180 s ceiling**; measured cost is 10–30 s, so this is headroom, not a target |
| 8 | Is masking required before samples reach the LLM | **yes, mask by default**; raw samples stay local |
| 9 | Sample size per table (default 200 rows) | 200 rows via `TABLESAMPLE ... REPEATABLE(seed)`, **never `TOP N`** |
| 10 | Run profiling on all low-score columns or top-N first | **all low-score columns** — measurement shows exact profiling is affordable |
| 12 | Profiling for >10 M-row tables | **exact, not approximate** — `APPROX_COUNT_DISTINCT` was slower and 2.3% wrong here; keep it only as a fallback for high-cardinality text |
| 13 | Sampling strategy | `TABLESAMPLE SYSTEM` with a stored seed; `TOP N` is ruled out as unrepresentative |

Decision 11 (wide-bundle fallback) is no longer needed: the Phase 2 addendum
raised `llm.timeout_s` to 1800 s instead of changing reasoning behaviour.

### Measured profiling cost (2026-11-13, `nge_small`)

Measured on the two largest tables rather than estimated, because the estimate
would have been wrong.

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

**Conclusion: exact profiling is affordable.** A full exact pass over the largest
rowstore table is roughly one scan (~6 s) plus a few distinct passes, so ~10 s;
the largest columnstore table is under 1 s. The 120 s budget was not too small —
it was roughly 10× larger than needed.

### Five findings that change the implementation

1. **`TOP (N)` is not a sample, and must not be used.** These tables are
   clustered on their identity column, which is monotonic in arrival order. The
   first 200 rows of `dw.fact_sales` are the *earliest transactions in the
   dataset*, so `TOP 200` would report a minimum date, a maximum date, a distinct
   count and a value distribution that are all wrong — while still looking
   plausible. Sampling must be `TABLESAMPLE SYSTEM/BERNOULLI` with
   `REPEATABLE(seed)`, which is also 3–100× faster than the full scan.

2. **`APPROX_COUNT_DISTINCT` is both less accurate and slower here.** On
   `transaction_line_id` it returned **25,225,206** against an exact
   **24,653,642** — a 2.3% overestimate — and took 2.4 s where the exact count
   took 11 ms, because the columnstore index is sorted on that column so the
   exact aggregate streams. Default to exact; treat approximate as a fallback for
   types where exact is genuinely expensive, and record which was used.

3. **`MIN`/`MAX` fail on `bit`.** SQL Server raises *"Operand data type bit is
   invalid for min operator"*. `bit` columns must be cast to `int`, or profiled
   for value distribution instead of range.

4. **`STDEV` on `decimal` is the single most expensive operation measured** —
   7,395 ms, twice the cost of scanning the table. Casting to `float` first cuts
   it to 1,082 ms. Numeric aggregate statistics should therefore run on a cast
   value, with the cast recorded so the precision loss is visible.

5. **Columnstore segment metadata cannot be trusted for profiling.**
   `sys.column_store_segments.min_data_id`/`max_data_id` hold *encoded* ids, not
   logical values: they reported `currency_code` as 0–1, `quantity` as 0–1, and
   `has_nulls = 1` on all 44 segments of `date_key`, a column the catalog says is
   `NOT NULL`. Free-looking shortcuts here produce confident nonsense.

Statistics histograms *are* usable (`sys.dm_db_stats_histogram`): first and last
`RANGE_HI_KEY` bound the range, and the sum of `distinct_range_rows` plus step
count approximates cardinality. They only cover the leading column of each
statistic, and sampled statistics are approximations, so they are a cheap
*tier 0* seed rather than a replacement for measurement.

### Revised approach: tiered, exact-first

| Tier | Source | Cost | Used for |
|---|---|---|---|
| 0 | Statistics histograms, `sys.partitions`/DMV | free | Seed min/max/cardinality/null estimate; exact row count |
| 1 | One batched scan per table | ~0.4 s (CS) / ~6 s (rowstore) | Exact count, null fraction, min/max per column |
| 2 | Exact `COUNT(DISTINCT)` per column | 11 ms–1 s | Distinct count, where the type allows |
| 3 | `TABLESAMPLE ... REPEATABLE(seed)` + top-k | 51–186 ms | Value distribution, top-k, patterns, PII, representative rows |

Degradation is *per metric, not per table*: if tier 2 would exceed the remaining
budget for a column, record `method='approximate'` (or `skipped` with a reason)
and continue. Each metric stores its own `method`, so a wrong-looking number can
always be traced to how it was obtained.

### Budget decision

Keep `profiling.max_runtime_per_table_s` as a **ceiling**, raised 120 → **180 s**
for headroom, but expect 10–30 s in practice. Add `profiling.exact_scan_max_rows`
(20M default) purely as a guard for a table far larger than anything measured
here, and `profiling.sample_percent` with the existing `REPEATABLE` seed.

### Known input from Phase 2

All 99 current columns sit in the `deep_profile` or `deep_profile_flagged` bands,
with `evidence_coverage` around 0.10 because `profile=0`. Phase 3 is therefore the
phase that can actually move these scores, and the before/after distribution is
the single most important number it produces.

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
- **Data source management** (see OUTLINE §24): encrypted credentials on
  `catalog.data_sources` (`0004`), a `TargetResolver` that reads targets from the
  database, the onboarding probe reused from `tools/check_target.py`, per-target
  limits, and an audit table for connection changes

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
| 15 | Admin-supplied connections: in Phase 4 or Phase 5 | **Phase 4** — the query path needs a target resolver anyway, and Phase 5's UI must not invent credentials storage |
| 16 | Credential encryption | **application-level AES-GCM**, master key in `.env`; `pgcrypto` is the fallback if key rotation outside the DB proves awkward |

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
| D12 | Conflict rule | Where documents disagree, `config/gaia.yaml` and OUTLINE.md win |
| D13 | Thinking | On for semantics and critique (config); per-run `--no-thinking` |
| D14 | Phase 2 inputs | Catalog facts only, English output, PII deferred to Phase 3 |
| D15 | Providers | DeepSeek = reasoning (Claude/Gemini may be evaluated later); Ollama = embeddings only |
| D16 | `join_hint` / `glossary_term` | FK = confirmed, model = inferred, SME never overwritten; glossary for human-in-the-loop / company glossary as ground truth |
| D17 | Wide tables | No chunking for now; token efficiency deferred to go-to-market tuning |
| D18 | Status bands | `auto_approved` ≥ `accept_at`, else `draft`; abstain band flagged for SME; tunable |
| D19 | LLM concurrency | **3**, one below the gateway's hard ceiling of 4 per model |
| D20 | Retry policy | Transient capacity (`429/502/503/504`) gets 8 attempts, 60 s cap, jitter, `Retry-After`; hard errors stay at 3 |
| D21 | Wide-bundle timeout | `llm.timeout_s` = **1800 s**; read timeouts retry once (`max_attempts_timeout: 2`); re-assess on provider change |
| D22 | `thinking` in skip key | **Deliberately excluded** — the default path always uses thinking; `--force` upgrades a table stored without it |

## Definition of done (per phase)

A phase is done only when all of the following hold:

1. Every acceptance criterion is ticked, and each tick corresponds to a command
   that was actually run — not to code that merely exists
2. Tests cover the new logic, and the full suite passes
3. `OUTLINE.md` records the verified results and any defects found
4. A short summary states what was built, what was proven, and what was deferred
5. Your gate is ticked

## Open questions blocking Phase 3

Phase 2 is closed, including the addendum. One item remains:

1. **Profiling budget.** Default is 120 s per table with samples of 200 rows.
   Several tables hold 24–26 M rows (`dw.fact_sales`, `dbo.transaction_lines`,
   `dw.sales_wide`). Confirm the budget, or approve approximate statistics
   (`APPROX_COUNT_DISTINCT`, `TABLESAMPLE`) for anything above a row threshold.

Previously open and now answered in the Phase 2 addendum:

| Was | Resolution |
|---|---|
| Thinking mode on wide bundles | `llm.timeout_s` 600 → 1800; read timeouts have their own retry budget |
| Is `thinking` part of the skip key? | No, and it stays that way — the default path always uses thinking, so the gap is deliberate |

