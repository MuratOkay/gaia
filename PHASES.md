# Gaia — Phase Plan (review & control)

> **Purpose.** A checkable plan for what gets built, in what order, and what
> must be true before each phase is considered finished. Use this to approve,
> reorder, or cut scope **before** work starts.
>
> This document is the control surface. `OUTLINE.md` is the design rationale;
> this file is the delivery contract.
>
> Last updated: 2026-11-18 (**the D88 host run triaged; build `2026-11-18 · D89 (D88 triage: host tests, regression runs, P6-1 by subject)`**) · Status: Phases 0–4 complete · **Phase 5 verified on the owner's host** — the owner's gate closes it · **Phase 6 in progress**: 6.0, 6A, 6B, 6C, 6D part 1, 6E, 6G's measurement built; the fresh net's acceptance test open (**P6-8**) · Owner checklist: [OWNER_VERIFICATION.md](OWNER_VERIFICATION.md) — **§30e is the next run**
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
| 3 | Evidence collection, profiling, views, scoring v2, re-reasoning, review queue | **DONE** — 3.P, 3.0, 3A (PostgreSQL), 3B; 3C accepted 2026-11-17 (P6-0): criteria 1–3 met, criterion 4 (the owner's audit ≥ 90 %) recorded as not met at 86 %; SQL Server residue tracked (D56) | ✅ approved 2026-11-14 · ✅ complete 2026-11-17 |
| 4 | Retrieval, SQL generation (**PostgreSQL first**, then T-SQL — D52), guarded execution, data-source management | **DONE** — 4A–4F verified on the host; golden run: executable 100 %, correct 97 % (targets 80 / 70); retrieval 95 % / 95 % (trial 90 / 80) | ✅ approved 2026-11-16 · ✅ complete 2026-11-17 |
| 5 | Sign-in through the company directory, four roles, need-to-know; SME review first, then chat; administration and audit (R1, scope revised 2026-11-17) | **VERIFIED ON THE HOST — awaiting the owner's gate**: 5A (real AD), 5A.2 (real tenant), 5B (g07 right by an SME decision alone), 5C–5E in the browser (D77–D80 runs); fixes D76–D81 | ✅ approved 2026-11-17 |
| 6 | Evaluation and calibration: one evaluation store and report, labels from people, calibrated column scores and answer confidence, regressions caught before shipping | **IN PROGRESS** — 6.0 (D78), 6A (the store, `make quality`, the page; D80, D81), 6E's replayed net (D82), 6B (labels, the blind share; D83; the 3C audit and the judge's κ, D85), 6C (calibrators, `make calibrate`; D86), 6D part 1 ("Was this right?", the SME's doubted answers, answer calibration; D87), 6E part 2's tooling (a deliberate regression), 6G's measurement (D88) built; 6A and 6B verified on the host (§28, §29); next the host run §29d, then 6F | ✅ approved 2026-11-17 |
| 7 | Masking/FPE, scheduled refresh, further dialects, workload evidence | **backlog** | ⬜ |

### Environment as it stands

| Component | State |
|---|---|
| Metadata store | PostgreSQL 18 + pgvector on `127.0.0.1:5433`, healthy |
| Extensions | `vector 0.8.7`, `pg_trgm`, `unaccent`, `btree_gin`, `pgcrypto`, `pg_stat_statements` |
| Schemas | `catalog`, `profiling`, `semantics`, `chat`, `evaluation`, `audit` |
| Redis | `127.0.0.1:6380`, healthy |
| Target | SQL Server 2025 CU7, `nge_small`, login `gaia_ro` (read-only, proven) |
| Catalog captured | 71 objects, 607 columns, 93 FKs, 166 indexes, **0 comments** |
| PostgreSQL dev target (3.P) | `demo_pg` on `127.0.0.1:5434` (Compose profile `target-pg`), synthetic, ~3 M rows at scale 1; role `gaia_ro` read-only proven from privileges. Verified in a sandbox on PostgreSQL 16; first run on the real host pending |
| PostgreSQL catalog captured | 28 objects (24 tables, 4 views), 213 columns, 3 comments |
| Reasoning LLM | `deepseek-flash` via the gateway since 2026-11-16 (host runs §13d on; before that `deepseek-v4.1-flash` — same model renamed or not: OWNER_VERIFICATION §21 Q3). JSON mode + thinking verified. Measured on the host: **~190 output tokens/s** with thinking (was ~15) — all 71 `nge_small` objects in 55 min, the 32-question golden run in 2 min |
| Gateway limits | 4 concurrent requests per model, 1200 req + 2.5M tokens per window, 15M/day |
| Embeddings | Ollama `bge-m3`, 1024 dims, verified |
| Semantics captured | host, 2026-11-16: `nge_small` all 71 objects / 607 columns (231 accepted; queue 289); `demo_pg` all 28 objects / 213 columns (108 accepted) |
| Tests | build D83: **734 passed, 10 skipped** in the sandbox with both PostgreSQL databases (skips: 8 SQL Server live, 2 Ollama live) — 744 expected on the host (D80 on the host: **735 passed, 0 skipped**); the web UI: 49 (vitest); the replayed golden net is part of it (6E)
| Sandbox (2026-11-16, D52) | Reproduces the PostgreSQL side end to end: `demo_pg` on PostgreSQL 16.15 (seeded at scale 1: 2,973,244 rows, same as the host), metadata store on PostgreSQL 16.2 + pgvector (no `pg_trgm`: the trigram indexes are the only difference). No SQL Server, no LLM gateway, no Ollama |
| Sandbox (2026-11-17, D76) | Also a **real LDAPS directory** with the Samba script's people and groups and a Samba-shaped certificate chain (`ldaptor`), a **headless Chromium** driving the web UI, and an OpenAI-compatible **scripted model** and embedding stub: sign-in, review, chat, refusals and the admin pages run through uvicorn and Vite as on the host. Still no SQL Server, no LLM gateway, no Docker
| Migrations | `0001`–`0019` (`0004` = 3B ledger and queue, `0005` = 3A profiling, `0006` = join-hint status `refuted`, `0007` = 4A `chat.query_run`, `0008` = D61 `chat.clarification`, `0009` = 4E `chat.conversation`, `chat.message`, `0010` = 4F data-source credentials, `audit.connection_change`, `0011` = D67 clarification option order, `0012` = 5A schema `auth` and `audit.auth_event`, `0013` = 5A.2 `auth.pending_login`, `0014` = 5B review signatures on `table_semantic` and `column_semantic`, `0015` = 5C categories and their grants, an SME's personal-data statement, 5D `query_run.asked_by` and the `refused` message, `0016` = D77 the basis of a category proposal, `0017` = D78 an account's identity, `0018` = D80 the evaluation store, `0019` = D81 a run's meanings and an item's SQL) |
| Web UI (Phase 5) | `frontend/`: Vite 8 + React 19 + TypeScript 5.9 + i18next, pnpm; `make web-install`, `make dev-web` (127.0.0.1:5173, proxies `/api`), `make web-test`, `make web-build` |

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
* ~~Adding definitions to the snapshot changes the view hash payload, so
  `STRUCTURE_HASH_VERSION` is bumped **2 → 3**: one re-baseline pass, after which
  every current semantic row is stale.~~ *Withdrawn in 3A (D32): definitions live
  in the profile store, so nothing is re-baselined; a changed definition
  re-reasons only the objects whose evidence it changes.*
* *3A status:* lineage, aliases (including single-column conversions) and the
  view-after-table order are built; pass-through columns are still reasoned in
  the view bundle rather than inheriting without a call, and computed view
  columns are not profiled (see "3A results", deferred items).

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

**3.0 — preconditions** · code complete; SQL Server runs pending (owner)
- ✅ `dialects/errors.py` — `StatementTimeout`, `LockTimeout`, raised by both dialects
- ✅ `dialects/mssql/connection.py` — hardened session; the statement timeout is
  now **applied** (ODBC query timeout, set before the cursor exists); HYT00 →
  `StatementTimeout`, native 1222 → `LockTimeout`. The catalog reader uses it.
  PostgreSQL sessions gained the same error translation and an autocommit mode
- ✅ **SQL Server probe hardening**: read-only proven from permissions (roles,
  `fn_my_permissions` allowlist, object-level writes, schema `ALTER`, procedure
  `EXECUTE`); the DDL attempt aims at the weakest schema; only a permission error
  counts as proof. 9 checks, as for PostgreSQL
- ✅ Single-column test: `test_single_column_change_reasons_exactly_one_bundle`
  (see finding 1 in "3.0 results"); `make test-db` creates the disposable database
- ✅ Measurement harness `tools/measure_target.py` (`make measure`) with SQL Server
  and PostgreSQL backends: every item below, in one unattended run
- ⏳ Owner: run the harness on SQL Server after a restart (cold cache) — covers the
  re-measurement of `dw.sales_wide`, non-key high-cardinality columns, percentiles,
  auto-created statistics and `TABLESAMPLE` on columnstore
- ⏳ Owner: `make check-target`, and `make test` with `GAIA_TEST_MSSQL=1`,
  `GAIA_TEST_DATABASE_URL` and `GAIA_TEST_PG_TARGET_URL` on the real host
  (includes the 3.P host verification)

**3A — evidence collection** (both dialects) · DONE in the sandbox (PostgreSQL); SQL Server runs pending (owner)
- ✅ `dialects/postgres/profiler.py`, `dialects/mssql/profiler.py` — every profiling
  statement of the dialect, module capture (`pg_get_viewdef`, materialized views,
  `pg_get_functiondef` / `sys.sql_modules`), privilege check; registered lazily in the
  dialect registry. *(Planned `modules.py` files folded into the profilers: one
  session, one place per dialect.)*
- ✅ `profiling/budget.py` (pages, size classes, per-table/per-run budgets, a time
  ceiling no statement can outlive, run window, freshness, catalog sufficiency),
  `profiling/statistics.py` (tiered profile; *the planned `sampler.py` is part of it*),
  `profiling/detectors.py`, `profiling/identities.py`, `profiling/joins.py`
  (candidates, row-weighted inclusion, specificity, uniqueness, exact top-k),
  `profiling/lineage.py` (view column lineage with `sqlglot`)
- ✅ `profiling/service.py` — Stage 1 and Stage 3 orchestration, one target session on
  one worker thread, advisory lock per data source, footprint report
- ✅ `db/models/profiling.py`, `db/repositories/profile_store.py` (persistence and the
  evidence loader reasoning uses) — `profile_run`, `table_profile`, `column_profile`
  (per-metric `method`, `rows_scanned`, `pages`, `runtime_ms`, `isolation`, `seed`),
  `verification_test`, `module_definition`, `view_column_lineage`
- ✅ `alembic/versions/0005_profiling.py` (R4: `0004` went to 3B). `STRUCTURE_HASH_VERSION`
  is **not** bumped — module definitions live in the profile store, not the snapshot (D32)
- ✅ `tools/profile.py` — `--target --schema --table --with-neighbours --limit --stage {1,3}
  --dry-run --ignore-window --force --no-joins --report --json`; `make profile`,
  `make profile-dry`, `make profile-tests`
- ✅ Reasoning reads the store by default when profiling is enabled for the target
  (`profiling.enabled: true`, `targets.<name>.profiling_enabled`); v2 prompts explain
  the new evidence (lineage, conversions, test origins, granularity)

**3B — reasoning with evidence** · DONE in the sandbox (owner request: before 3A, see "3B results")
- ✅ `profiling/summary.py` — the **evidence contract** 3A must fill
  (`ColumnProfileSummary`, `DetectorHit`, `IdentityRelation`, `ViewAlias`,
  `VerificationResult`, `TableEvidenceInput`); sources `NoEvidence`,
  `StaticEvidence`, `JsonFileEvidence`
- ✅ `semantics/evidence.py` — rendering (rounded; volatile detail shown but not
  hashed), exposure (`raw` / `masked` shapes), evidence refs, evidence hash,
  ledger, profile checks, coverage, `next_stage`
- ✅ `semantics/scoring.py` → `score_column_v2` (`heuristic-v2`); v1 unchanged and
  selectable (`scoring.version`)
- ✅ `semantics/prompts/reason_table.v2.md`, `critique_table.v2.md`
  (`semantics.prompt_major: 2`); `semantic_type` in the answer
- ✅ `semantics/chunking.py` — column chunks under `llm.context_budget_tokens` and
  `max_output_tokens`; merge of answers and critiques
- ✅ `semantics/service.py` — evidence hash in the skip key, critique-missing
  re-run, views after tables, v2 scoring, ledger + review persistence, coverage
- ✅ `semantics/review.py`, `tools/review.py` (`make review`) — the SME queue
- ✅ `llm/replay.py` — file-based provider (`--replay DIR`; recorded as model `replay`)
- ✅ `alembic/versions/0004_evidence_review.py` — `evidence_item`, `review_item`,
  new columns (3A's profiling tables take the next number, R4)

**3C — evaluation** · harness built (2026-11-15); runs pending (owner, LLM gateway)
- ✅ `tools/evaluate_semantics.py` (`make evaluate-plan | evaluate-labels | evaluate |
  evaluate-judge | evaluate-audit | evaluate-report | evaluate-cost`) and the
  `gaia.evaluation` package: `blind.py` (name-blind worlds), `runner.py` (conditions,
  rounds, scoring), `stage3.py` (claim tests of each condition, in memory), `judge.py` +
  `semantics/prompts/judge_meaning.v1.md`, `metrics.py` (accuracy, AUROC, precision of
  accepted), `labels.py` (stratified template, loader), `report.py` (criteria, audit),
  `recorder.py` (every answer on disk: resumable), `cost.py` (criterion 5)
- ✅ `infra/pg-target/labels.csv` — ground truth of the 24 worst-case `demo_pg` columns
- ⏳ A stratified set of 40 columns labelled by the owner (`make evaluate-labels`)

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
- [x] Same on the real host with `postgres:18` (owner, 2026-11-15: PostgreSQL 18.7; `check-target`
      9/9, introspection 28/213, every PostgreSQL live test passes, replay reproduces)

**3.0** — ✔ = verified in the sandbox; SQL Server items need the owner's run
- [x] A PostgreSQL statement exceeding the timeout is cancelled, raised as
      `StatementTimeout`, and the session survives (live test)
- [x] The same on SQL Server — owner, 2026-11-15: `WAITFOR` cancelled as `StatementTimeout`,
      session survives (`test_mssql_target_live.py`)
- [x] The SQL Server probe fails for every write path (sysadmin, writer roles,
      non-read database permissions, object writes, schema `ALTER`, procedure
      `EXECUTE`), aims its DDL at the weakest schema, and treats only permission
      errors as proof — unit-tested; [x] live pass for `gaia_ro` (owner, 2026-11-15:
      `check-target` 9/9, `write denied as expected in schema dbo (error 262)`)
- [x] The single-column re-reasoning test exists and passes
- [x] Re-measurement table added below, including `dw.sales_wide` and cold cache —
      PostgreSQL (scale 10) and SQL Server (owner, 2026-11-15; "Owner verification")
- [x] Auto-statistics and columnstore-sampling behaviour verified and recorded —
      PostgreSQL (no footprint) and SQL Server (43 statistics in the measurement;
      `TABLESAMPLE` supported on clustered and nonclustered columnstore)

**3A** — ✔ = verified in the sandbox on PostgreSQL (`test_profiling_pg.py` and the runs
in "3A results"); SQL Server needs the owner's run (OWNER_VERIFICATION §7–§9)
- [x] Every in-scope table has a profile or a recorded skip reason
      (`catalog_sufficient`, `no_select`, `budget`, `window`, `view`,
      `privilege_check_failed`)
- [x] Every metric records `method`, `rows_scanned`, `pages`, `runtime_ms`,
      `isolation`, and `seed` where sampled (shared scans and samples say so)
- [x] No generated profiler statement contains an unordered `TOP` used as a
      sample — sqlglot test over every statement of both dialects, plus a test
      that the rule itself catches one (`test_profiler_sql.py`)
- [x] No table exceeds its page budget or the 180 s ceiling; degradation is recorded
      per metric (`test_statistics.py`, `test_budget.py`; live: max 4.0 s per table)
- [x] Detectors pass positive/negative fixtures, including checksum validators
- [x] Every view column is classified pass-through / computed / unresolved;
      the three `rpt` views (and the materialized view) checked by hand — 28 of 28 right
- [x] Every inferred join hint has an inclusion-test result (single-column hints;
      composite hints are reported as untested) — failed on the owner's host at first (D1);
      after the fix every hint has a result on both targets (owner, §11c: `f2`, `f9`,
      `condition` FAIL 0 % and `refuted`, no errors)
- [x] A second run inside the freshness window issues **zero** data queries
      (asserted on the recorded statements)
- [x] The footprint report lists auto-created statistics attributable to the run —
      PostgreSQL none; SQL Server 10 for the Phase 2 scope, 129 for the full run (owner)
- [x] With `SELECT` denied (simulated), the run completes in catalog-only mode
- [x] The same on SQL Server: the live tests in `test_mssql_target_live.py` and
      `make profile` on `demo_mssql` (owner, §11a/§11b: 378 passed; 71 objects, 65
      profiled, 1 `partial` with its reasons printed, none `failed`)

**3B** — verified in the sandbox (unit + PostgreSQL integration tests, replay run)
- [x] Scoring unit tests prove: a contradiction lowers the score below the
      no-evidence baseline; being profiled without passing checks changes
      nothing; names alone cannot reach `accept_at`; specific evidence can;
      breakdown still sums exactly to `raw_score` (`test_scoring_v2.py`)
- [x] The ledger is stored for every scored version (`evidence_item` + breakdown)
- [x] Re-running with unchanged evidence issues **zero** LLM calls; changing one
      table's evidence re-reasons exactly that table; sub-bucket noise re-reasons
      nothing (`test_reasoning_v2_pg.py`)
- [x] A bundle over `context_budget_tokens` is chunked and validates
- [x] In `masked` mode no literal sample value appears in any rendered prompt;
      the raw override does show them
- [x] Every column below `accept_at` with exhausted coverage has a review item;
      `unused` columns are flagged and excluded; one open item per column
- [x] With the configured model on the owner's host (not the replay) — owner,
      2026-11-15: §6b (evidence file) and §7–§8 (collected evidence), 52 calls; every
      call listed with `--cost-report` (42) validated on the first attempt

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

### 3.0 results (2026-11-14, sandbox; SQL Server runs pending)

Code: `dialects/errors.py`, `dialects/mssql/connection.py`, the hardened
`dialects/mssql/probe.py`, `dialects/{mssql,postgres}/measure.py`,
`tools/measure_target.py`; tests `test_mssql_session_and_probe.py` (24),
`test_measure_harness.py` (13), the single-column pipeline test, and live tests
for both dialects. Suite: 251 passed, 5 skipped (Ollama live, SQL Server live);
without the opt-in databases 239 passed, 17 skipped.

Findings:

1. **The "missing" single-column test existed but never ran on the host.** It was
   part of `test_pipeline_end_to_end`, which skips without `GAIA_TEST_DATABASE_URL`;
   the host's "1 skipped" was almost certainly that test, hence the partial tick
   in Phase 2. It is now a dedicated test that also proves other tables keep their
   version, data-only movement re-reasons nothing, and a comment-only edit
   re-reasons exactly one table. `make test-db` makes the database trivial to create.
2. **The SQL Server probe counted any error as proof of read-only.** A syntax
   error, a timeout or a dropped connection during the `CREATE TABLE` attempt
   would have passed. Only permission errors count now, and read-only is also
   proven from permissions (including `EXECUTE` on procedures, which can write
   through ownership chaining).
3. **The SQL Server statement timeout was configured but never applied** — the
   catalog reader's docstring claimed otherwise since Phase 1. Applied now, set
   before the cursor is created (pyodbc applies it to cursors created later).
4. Running a single test file from `backend/` trips a pytest-asyncio assertion
   (pre-existing, also at HEAD); `make test` now runs from the repository root,
   so `make test ARGS="tests/…"` works.

PostgreSQL measurements — `demo_pg` at scale 10 (29 M rows, 4 GB), sandbox with
4 vCPU, `max_parallel_workers_per_gather = 2`, default `work_mem`, PostgreSQL
buffers cold after a restart but OS cache warm; runs vary ±25 %. Full report:
[`docs/measurements/2026-11-14-demo_pg-scale10-sandbox.md`](docs/measurements/2026-11-14-demo_pg-scale10-sandbox.md).

| Measurement (2.5–6.7 M-row tables, 45–67 k pages) | Result |
|---|---|
| `count(*)` / one batched min-max-count scan of all columns | 0.2–1.9 s / 1.2–2.6 s — one pass, cheap |
| Exact `count(DISTINCT)` per non-key column | median 1.6–4.3 s, worst 4.7–11.6 s (1–2 M distinct); spills to temp up to 63 k pages — as much as the table |
| Exact tier 1+2 per table (count + scan + every distinct) | **28–60 s** at 6.7 M rows; linear extrapolation to 24 M rows ≈ 2–3.5 min, **over the 180 s ceiling** |
| `count(DISTINCT)` vs `GROUP BY` form | low cardinality: GROUP BY **2.4–7×** faster (hash instead of sort); index-leading or high cardinality: `count(DISTINCT)` equal or up to 3.4× faster |
| `pg_stats` `n_distinct` vs exact | exact for low cardinality; up to **98.6 %** off for high cardinality |
| `AVG`+`STDEV` native vs `float8` cast | `numeric`: no gain (0.7–1.2×); `bigint`: **2.2–2.6×** faster (PostgreSQL averages integers in `numeric`) |
| Exact median vs median of a 10 k-row `SYSTEM` sample | 1.9–4.5 s vs 5–12 ms; value within ~2 % |
| `TABLESAMPLE SYSTEM` 10 k rows | 81–154 pages, ≤ 20 ms, 87–97 % of the requested rows |
| `TABLESAMPLE BERNOULLI` 10 k rows | reads every page (44–67 k), 117–345 ms |
| Cold vs warm | up to 14× on one column (`customer_id` 11.3 s cold vs 0.8 s warm) |
| Footprint | no statistics created (inventory identical before and after) |
| `TABLESAMPLE` support | partitioned table ✔, materialized view ✔, view ✘ |

Inputs to 3A (recorded here, decided at the 3A gate):

* **Exact-everything does not fit a large table.** This confirms the P3-5 size
  classes: exact only for the small class; for large tables, tier 1 (one
  batched scan) exact, distinct counts per metric by strategy, degraded and
  labelled when the budget runs out.
* **Choose the distinct strategy per column**, seeded by tier 0: `GROUP BY` form
  (with `IS NOT NULL`) for low estimated cardinality, `count(DISTINCT)` for
  index-leading columns, sampled/approximate for high-cardinality non-indexed
  columns. The planner estimate decides the *strategy*, never the *result*.
* **Casts are dialect- and type-specific.** PostgreSQL: cast integers, not
  decimals. SQL Server: pending the like-for-like re-measurement.
* **Budgets must count temp pages.** Exact sorts spill as many pages as they read.
  Whether to raise `work_mem` per profiling session (a per-target limit) is a 3A
  decision; it trades spill for memory on the customer's server.
* **`BERNOULLI` has no role on large tables** (it reads everything); `SYSTEM` plus
  exact confirmation in Stage 3 stays the design.
* **Budget for a cold cache**: production caches will not hold the profiled tables.

### 3B results (2026-11-14, sandbox)

**Ordering.** The owner asked for 3B before 3A. 3B is the *consumer* of evidence,
3A the *producer*, so 3B was built against an explicit contract
(`profiling/summary.py`). Today the evidence is catalog-only by default; profiles
can be supplied as JSON (`--evidence-file`) until 3A's profiler fills the same
contract. With `profiling.enabled: false` (until 3A), catalog coverage is final
and sub-accept columns go straight to the review queue — the degraded mode of
Stage 1.

**Tests.** 293 passed, 5 skipped (Ollama live, SQL Server live); new:
`test_evidence.py` (17), `test_scoring_v2.py` (10), `test_chunking.py` (6),
`test_replay_and_review.py` (3), `test_reasoning_v2_pg.py` (7). Migrations
`0001`–`0004` produce DDL identical to the models (pg_dump diff).

**Replay smoke test.** The assistant acted as the model through the replay
provider on three `demo_pg` tables (`legacy.tb_trx02`, `legacy.tb_mst01`,
`hr.departments`), with evidence from a throw-away profiling script.
All six answers validated on the first attempt;
21 columns scored: 10 accepted, 11 queued. In the first run (scale 10) the queue
was led by the two columns with real counter-evidence (a "credit limit" stored
as text; "names" with 0.8 % distinct); the committed fixtures (scale 1, where the
names are 8 % distinct and only plausible) lead with the credit limit and with a
reference that has no FK yet. **This is a functional test, not accuracy:** the assistant designed
`demo_pg` and knew every answer. Fixtures and an integrity note:
`tests/fixtures/replay_3b/`.

Findings from the smoke test, each fixed and covered by a test:

1. **Volatile detail churned the evidence hash.** An identifier's maximum, the
   examples and the top values of a free column change with every insert; they
   were hashed, so live tables would re-reason after nearly every load. They are
   still shown to the model but excluded from the hash; top values that carry no
   information (each ~0.0 %) are no longer rendered.
2. **Generic detections contradicted specific claims.** A phone column also
   validated as `numeric_text`, and a correct `phone` claim was counted as
   contradicted. Generic types are now subsumed by specific ones.
3. **A failed critique was never retried.** The table was skipped as
   "unchanged" on every later run and its columns could never be approved — a
   Phase 2 defect, too. Such versions now re-run ("critique missing").
4. **Name-based rules ignored view aliases.** A reference without a declared FK
   failed the role check on a meaningless name (`f2`) although its alias is
   `customer_code`. Aliases now count as names for those rules (A8).
5. **Integer times were judged as date keys.** `hhmmss` values failed the
   `yyyymmdd` check; the claimed `semantic_type` now selects the check.

Also found, for 3A: a small table sampled with `TABLESAMPLE` returned no rows, so
its lengths read 0 — small tables must be scanned, never sampled (already the
P3-5 size-class rule).

Known limits:

* Rounding buckets have edges: a tiny change that crosses one (8.4 % → 8.6 %
  nulls) counts as new evidence and re-reasons one table. Accepted cost.
* Changing scoring weights or strengths does not re-score stored versions
  without `--force` (the scorer is not in the skip key, as in Phase 2). A
  re-score-only command that needs no LLM is a candidate for 3C.
* Views are reasoned after tables, but computed view columns only get their
  inputs' meaning once 3A's lineage exists. *(3A: lineage exists.)*
* Until 3A, no table has `tests_pending`, so Stage 3 never runs; references
  without FKs (like `f2`) stay in the queue until an inclusion test exists.
  *(3A: Stage 3 exists; `f2` gets a Stage 1 inclusion test.)*

### 3A results (2026-11-14, sandbox, PostgreSQL 16, `demo_pg` scale 1)

**Ordering.** Built after 3B (D29), against the contract 3B consumes; 3B's
reasoning now reads the profile store by default. No LLM was involved in any
number below.

**Tests.** 365 in total: 357 pass and 8 skip in the sandbox with both
PostgreSQL databases (the skips need Ollama or SQL Server). New: `test_statistics.py`
(9), `test_joins.py` (3), `test_budget.py` (6), `test_profiler_sql.py` (6),
`test_mssql_profiler.py` (1), `test_evidence.py` (+4), `test_scoring_v2.py` (+1),
`test_detectors.py` (26),
`test_identities.py` (3), `integration/test_profiling_pg.py` (4),
`integration/test_replay_fixture_pg.py` (1), and three live SQL Server profiler tests
for the owner. Migrations `0001`–`0005` produce DDL identical to the models.

**Stage 1 on the whole database** (`make profile TARGET=demo_pg`, 259 MB of tables):

| | First run | Second run |
|---|---|---|
| Tables | 24 profiled, 4 views skipped (`view`) | 24 reused, 4 skipped |
| Data queries | 169 | **0** |
| Catalog queries | 25 (modules, privileges) | 1 (modules) |
| Pages (estimated, logical) | ~331 k (≈ 10 × the heap: per-column exact distinct counts on every *small* table) | 0 |
| Wall time | 25 s; slowest table 4.0 s (`sales.orders`, 19 queries) | 0.5 s |
| Footprint | no statistics created | — |

View lineage: 16 pass-through, 12 computed, 0 unresolved — all 28 checked by hand
against `schema.sql`. Join candidates: 17 tested, 8 kept, 6 passed; the ones that
matter for the worst case are `legacy.tb_trx02.f2 → crm.customers.customer_code`
(99 % of rows), `f9 → store.stores.store_code` (98 % of rows, although two thirds of
its distinct codes are orphans), `f10 → finance.currencies.currency_code`, and
`finance.gl_entries.doc_ref → finance.invoices.invoice_no` kept at 90 % (a
polymorphic reference: the rest are payroll documents).

What the model now sees for `legacy.tb_trx02` (meaningless names): a profile per
column, `f3` detected as `date_int` **and** aliased by the view as
`transaction_date` *via* `TO_DATE(…, 'YYYYMMDD')`, the identity `f7 = f5 × f6`,
aliases for eight of ten columns, `f4` detected as `time_int` and — correctly —
**no** join candidate, and inclusion tests for `f2`, `f9`, `f10`.

**The same answers, scored with 3A evidence** (functional check, not accuracy).
The six replay answers of the 3B fixtures were re-run unchanged, but with the
evidence this profiler collected instead of the fixture's throw-away JSON. Because
the answers are identical, every difference is the evidence's doing:

| Column (claim) | Fixture evidence | 3A evidence | Why |
|---|---|---|---|
| `tb_trx02.f2` (reference) | 0.754 deep_profile | **0.901 accepted** | inclusion: 99 % of rows in `crm.customers.customer_code` |
| `tb_trx02.f9` (code) | 0.806 | 0.916 | inclusion: 98 % of rows in `store.stores.store_code` |
| `tb_trx02.f10` (code) | 0.900 | 0.942 | inclusion in `finance.currencies` |
| `tb_mst01.c01` (identifier) | 0.585 flagged | 0.769 | its values are `crm.customers` codes |
| `tb_trx02.f4` (timestamp) | 0.663 | 0.792 | `time_int` validated in 100 % of sampled values (the fixture's script: 84 %, a weak hit) |
| `tb_mst01.c07` (measure) | 0.327 abstain | 0.347 abstain | still contradicted: a "credit limit" stored as text |
| the other 15 | — | unchanged | no new specific evidence |

Accepted 10 → 11, review queue 11 → 10 (`f2` left it); no score went down. The
integrity note of the fixtures applies: the assistant wrote those answers and
designed the database.

**Design corrections found while building** (each covered by a test):

1. **Integer inclusion is chance on dense keys.** `f4` (a time of day, 80000–215959)
   "passed" inclusion in `sales.orders.order_id` and `sales.payments.payment_id`;
   `products.weight_g` in `crm.customers.customer_id`. A dense key contains every
   integer in its range. Specificity is now computed (chance = density^values,
   must be < 1 %); non-specific pairs are not even Stage 1 candidates, and a
   non-specific Stage 3 test is weak support that never contradicts.
2. **Distinct-value rates punish legacy orphans.** `f9` has 40 valid store codes
   and ~100 rare invalid ones: 29 % of values, 98 % of rows. Inclusion is now
   row-weighted; the distinct share is reported alongside.
3. **The most frequent values are not a sample of rows.** Testing the top 200
   values of `doc_ref` tested only payroll documents. Values for inclusion are now a
   seeded random prefix of the row sample — unbiased for the row-weighted rate.
4. **One heavy value is a coincidence.** `tb_prm03.grp` "found" 67 % of rows in
   `hr.departments.dept_code` because one group code (`STR`) is also a department
   code. A kept result now needs half the rows *and* half the distinct values.
5. **Small tables were not exact.** P3-5 says small tables are exact; high-cardinality
   columns had used the planner's estimate. Now one `GROUP BY` count per column.
   This is where the ~10 × page figure comes from, and the first budget to tune
   with the SQL Server measurement (`full_scan_max_pages`).
6. **Views that convert a column are aliases too.** `transaction_date =
   TO_DATE(f3::text, …)` was "computed", so the one business name `f3` has never
   reached it. Single-column conversions (casts, `to_date`, `trim`, `coalesce` with
   a constant) now also give the base column an alias, with the expression.
7. **A statement could outlive the ceiling.** A query started at 179 s could run
   for its 60 s timeout. A statement now starts only if it would end, even at its
   timeout, within `max_runtime_per_table_s`.
8. **A failed privilege check is not a missing grant.** It was recorded as
   `no_select` (final); now `privilege_check_failed`, retried next run.
9. **Large tables on SQL Server: one approximate scan, not one per column.**
   `APPROX_COUNT_DISTINCT` for every high-cardinality column runs in one batched
   statement.
10. **Stage 3 routing.** "Tests pending" was a table flag nothing set. It is now
    derived per column from the claim (`pending_claim_tests`), so a column with a
    test still to run is escalated, not queued, and Stage 3 runs exactly those tests.
11. **Type fit from data, not only from names** (`heuristic-v2` only; v1 untouched).
    An integer claimed as a timestamp failed the type check unless its *name* looked
    like a date — with meaningless names it always failed, although the detector had
    validated every value as `yyyymmdd`. The check now also passes on a strong
    `date_int`/`time_int` detection, a passed specific inclusion test (reference) or
    unique values (identifier). Under the fixture evidence this raised `f3`, `c05`
    and `c01` within their bands; the replayed bands and queue are unchanged.

Replay fixtures: the prompts gained explanations of the new evidence, which
changed every request key; the six answers were moved to the new keys unchanged
and an integration test now fails whenever they stop reproducing.

**Known limits and deferred items** (none blocks 3C):

* **Integer surrogate references without FKs cannot be confirmed by inclusion** when
  the parent key is dense — the common case. They rest on names, aliases and the
  model's proposal, and their Stage 3 test is weak. Query Store join mining
  (backlog) is the evidence that would settle them.
* Pass-through view columns are reasoned like other view columns instead of
  inheriting the base column's meaning; computed view columns are not profiled.
  Materialized views are skipped like views.
* Statistics histograms (tier 0), index usage (importance), numeric mean/stddev,
  fan-out tests and exact range tests for *huge* tables are not implemented.
  Mean/stddev were dropped on purpose: they cost a scan (STDEV on decimals was the
  slowest operation measured) and say little about meaning. Temporal granularity
  is implemented.
* Low-cardinality top-k on *large* tables is one `GROUP BY` per column; batching
  them (`GROUPING SETS`) is the next saving if the SQL Server measurement shows it
  matters.
* Stage 1 join candidates are evidence, not join paths: they reach `join_hint` only
  when the model (or an SME) proposes the join. Whether Phase 4 should also use
  passed candidates directly is a Phase 4 question.
* `TABLESAMPLE` on clustered columnstore and the SQL Server footprint are verified
  by the owner's run; if `TABLESAMPLE` is refused, the metric degrades with the
  error recorded (the batched scan still runs).
* Page counts are estimates from catalog sizes, not measured I/O.

### Owner verification (2026-11-15) and fixes

The owner ran OWNER_VERIFICATION §1–§9 on the host (PostgreSQL 18.7 target,
SQL Server 2025 `nge_small`, DeepSeek gateway, Ollama). Outputs:
`verification/`. §4 and §9 were repeated (the first §4 output was the PostgreSQL
target; the first §9 run had the placeholder passwords in both URLs).

**What held.** Replay (§6a) reproduced exactly on PostgreSQL 18 — so 18 renders
the bundles byte-identically to 16. Stage 1 on `demo_pg` matched the sandbox
(169 data queries, candidates 17/8/6, lineage 16/12/0, 13 s); both dialects
reuse with zero data queries. With the configured model, `tb_trx02` (meaningless
names) got 8 of 10 columns accepted on view aliases, the `f5 × f6` identity and
detectors. Names alone never exceeded 0.623 (cap 0.65). Suite on the host: every
PostgreSQL live test passes; SQL Server live tests 5 of 6 (D4).

**SQL Server measurement** (cold after `docker restart`; 24.7 M rows each;
[report](verification/gaia-measure-demo_mssql-2026-11-14T215622.md)):

| | `dbo.transaction_lines` (rowstore) | `dw.fact_sales` (NCCI) | `dw.sales_wide` (CCI) |
|---|---|---|---|
| exact tier 1+2 | 58.7 s | 45.4 s | 96.0 s |
| batched min/max/count scan | 16.1 s | 26.9 s | **60.8 s** (= the 60 s profiling timeout) |
| non-key exact `COUNT(DISTINCT)`, median / worst | 3.2 / 7.0 s | 0.8 / 2.6 s | 0.4 / 11.6 s |
| exact ÷ approx time (median) | 1.48 | 0.88 | 0.30 — exact is cheaper on columnstore |
| `APPROX_COUNT_DISTINCT` error, median / max | 0.5 / 2.3 % | 0.2 / 2.3 % | 0.0 / 3.2 % |
| median: exact / approx / 10 k-row sample | 165 s / 9.6 s / 0.1 s | 23 s / 6.2 s / 0.06 s | 21 s / 4.2 s / 0.2 s |
| `TABLESAMPLE` 10 k rows: rows, pages | 6,624, 18 | 9,766, 18,458 | 9,451, 375 |
| STDEV native vs float cast | 1.1× | 1.1–1.3× | 1.4–1.5× |

Compatibility level 130; footprint 43 statistics (measurement), 10 (Phase 2
scope), 129 (all 71 objects). A full Stage 1 of the database: 387 data queries,
270 s; three tables `partial`, all by the per-table page budget (below).

**Defects found in the owner's outputs** (D-numbers as discussed with the owner):

| # | Defect | Status |
|---|---|---|
| D1 | A claimed join from a code column to an integer key (`f2 → customers.customer_id`, `condition → dim_condition.condition_key`) raised a conversion error; nothing was recorded, so the claim stayed *pending* forever and the column could never reach the queue | **fixed**: values are converted to the key's family, non-convertible values count as not found, the test fails; a test that cannot run (budget, timeout, vanished target) is recorded as *inconclusive* (`method = not_run`), not shown to the model, and tags the review item `test_not_run`; driver errors are stored without the values they quote |
| D2 | Model join hints never expired: an earlier answer's `f2 → customer_id` stayed `inferred` (and was tested) after the model moved to `customer_code`; a failed test did not demote its hint | **fixed**: re-reasoning a table stales the hints its new answer drops (and revives re-proposed stale ones); a claim test finding < 50 % of rows marks the hint `refuted` (migration `0006`), a later pass restores it |
| D3 | Dense-key specificity (D34) was computed for a 100 % outcome while tests pass at 95 % and are kept at 50 %: a count "passed" into `hr.payroll_lines.payroll_line_id` (99 %) | **fixed**: the pre-test filter stays; after the test, an integer result is specific only if chance would reach that many hits < 1 % (binomial tail) |
| D6 | Stored `semantic_type` depended on detector order: a phone column was stored as `numeric_text` | **fixed**: a specific type wins over a generic carrier |
| D8 | Stage 3 inclusion used the 200 most frequent values (the bias 3A correction 3 removed from Stage 1) | **fixed**: Stage 3 draws its values from the sample in seeded random order, like Stage 1 |
| D10 | A declared FK contradicted a `code` claim (0.50) while a passed inclusion test supported it: `payments.currency_code` 0.61 as a code vs 0.78 as a reference (query 4: exactly four columns) | **fixed, default pending owner confirmation**: FK and inclusion support `reference`, `identifier`, `code`; say nothing about `dimension`, `flag`; contradict the rest. heuristic-v2 only |
| D11 | The object list showed primary keys only, so the model aimed code columns at surrogate ids (5 of 14 model hints were type-mismatched) | **fixed**: generation-2 prompts list unique keys too (`crm.customers (pk: customer_id; unique: customer_code)`); not in the skip key, so nothing re-reasons; replay fixtures re-keyed |
| D4 | Large columnstore table: the batched scan (60 s) hits the statement timeout, and the rule "start only what would end within the ceiling at its timeout" then refuses the 0.2 s sample → `failed` under a 120 s ceiling | **fixed**: the sample runs first; on columnstore, one aggregate statement per column (and exact per-column distinct counts, cheaper there than the approximation) replaces the wide scan, with per-column page estimates |
| D5 | Degradations per metric (all 23: per-table page budget, query 3) are stored but not printed; page estimates use the whole table although an index or columnstore segment is read | **fixed**: every `partial` table prints one note per metric and reason; the low-cardinality top-k of a large rowstore table run in **one** `GROUPING SETS` scan instead of one scan each (the cause of all 23); columnstore estimates per column. Index-aware estimates for rowstore: not built (the catalog has no index sizes) |
| D12 | Join candidates are discovered only among tables profiled in the same run (sample values are not stored), so references from an earlier run's tables to later-profiled parents were never tested | **fixed**: tables profiled earlier are paired with the parents profiled now; a pre-filter on their stored profiles decides which to sample again (one statement per table), and pairs already tested are not repeated |
| D7 | Empty strings counted as non-numeric, so a correct "credit limit" abstained | **fixed** (owner decision): blank text is NULL in every metric, detector and exact count; the profile says how much of the null share was blank (`nulls_blank_text`). `tb_mst01.c07`: numeric 88 % → 100 %, 0.327 abstain → 0.642 on the replayed answer |
| D9 | Adopting a Stage 1 test as the test of a claim re-reasoned with identical facts | **fixed** (owner decision): the origin leaves the evidence hash; `make rescore` and automatic re-scoring (below) |

**Owner run of §11 (2026-11-15, no LLM; 11e deferred: gateway slow).** 378
passed. SQL Server Stage 1 over all 71 objects: 65 profiled, 5 skipped, **1
partial** (was 3) in 508 s; `dbo.transactions`, `dbo.transaction_lines`,
`dbo.inventory_movements` now complete with 4 statements each (one `GROUPING
SETS` scan works on SQL Server); `dw.fact_sales` profiled column by column (38
statements, 57 s); footprint 13 statistics. Stage 3: the three errored claims
FAIL at 0 % and are `refuted`. Re-scoring: 9 + 10 tables, 0 calls; the
pass-through columns of `v_legacy_transactions` left the queue (37 → 32 items on
`demo_pg`), `c07` is no longer `abstain`, the four FK code columns lost their
contradiction. Three things it showed, fixed the same day:

| # | Finding | Fix |
|---|---|---|
| D49 | `tb_trx02.f2` and `f9` were queued with a **contradiction** (0.62, 0.56): the refuted claim `f2 → customer_id` contradicted "reference" although `f2 → customer_code` had passed | a failed test of one parent no longer contradicts a reference whose values were found in another key; the refuted parent is a fact about the hint (D2). `SCORER_REVISION` v2.2, so the next re-score applies it (sandbox: `f2` 0.621 → 0.901) |
| D3b | `line_count → hr.payroll_lines.payroll_line_id` (99 %) and `units → …customer_key` were still kept: the binomial check used the key's density over its whole range, but ids can be dense where the counts lie and sparse elsewhere | the density is measured where the tested values lie: one index range count on the parent key (`range_count_sql`) |
| D4b | `dw.sales_wide` still `partial`: 28 column statements (~4 s each, cold) and then the time rule refused the 25 exact distinct counts | the exact distinct count of a high-cardinality columnstore column is read in the same statement as its aggregates (one read of its segments, not two). The time rule itself became adaptive (D50, owner decision): statements are planned at twice the slowest so far, not at the full timeout |

**Owner decisions of 2026-11-15, as built.**

* *Re-scoring without the model* (decision 1). A table is re-reasoned only when
  the model would see something new. When only what the scorer reads has
  changed — the scorer (`SCORER_REVISION`), scoring config, a test's origin,
  an inherited column's base — `make reason` re-scores the stored answer
  instead (action `rescored`, a new version, no LLM call). `make rescore`
  never calls the model: it also re-scores tables the model would re-read, as
  an interim view, and keeps their answer's evidence hash so `make reason`
  still re-reads them. The upgrade itself re-reasons nothing: versions hashed
  before the origin left the hash still match (`legacy_hash`).
* *Blank text is NULL* (decision 2): D7 above.
* *Pass-through view columns are not reasoned* (decision 3). They take their
  base column's current meaning and score (following view-on-view lineage to
  the table), cite `lin:`, are never queued, and are refreshed by re-scoring
  when the base changes. Only computed columns go to the model, with the rest as
  context (like a chunk); a view made only of pass-through columns costs no
  call. On `demo_pg` that leaves one of the eight `v_legacy_transactions`
  columns for the model; in the owner's run 26 of 37 queue items were view
  columns.
* *FKs and roles* (decision 4, delegated): D40 below, now final.
* *Join discovery across runs* (decision 5): D12 above.
* *Phase 4 uses tested model hints* (decision 6): D48; the UI shows declared
  and discovered joins separately.
* *Identities on nge_small* (owner's answer): the computed values are USD
  conversions (local ÷ rate) with the rate in another table, so no within-row
  identity exists to find; division within one row is already covered
  (`c = a ÷ b` is found as `a = b × c`). Those measures rest on the model, the
  critique and the SME.

**Tests.** 372 passed, 8 skipped in the sandbox with both PostgreSQL databases
(skips: 6 SQL Server live, 2 Ollama). New: `test_joins.py` (+2), `test_evidence.py`
(+3), `test_scoring_v2.py` (+1), `test_semantics_bundle.py` (+1), `test_statistics.py`
(+4: blanks, `GROUPING SETS`, columnstore, sample first), `test_profiling_pg.py` (+3:
hint lifecycle and not-run tests; view inheritance and re-scoring; join
candidates across runs). Migrations `0001`–`0006` give DDL identical to the models (pg_dump diff);
`0006` downgrades and upgrades cleanly. Running the owner's model hints through
Stage 3 on `demo_pg` now gives `f2 → customer_id` and `f9 → store_id` FAIL 0 %
(`refuted`), the other five as before, and no error.

### 3C harness (2026-11-15, sandbox)

Built against "Evaluation design" above; what the design left open, decided
here (D51):

* **Every condition runs today's pipeline.** N-C is run again with the v2 prompts
  and no evidence instead of reusing the stored Phase 2 answers, so N-C vs N-P
  measures the evidence, not a prompt generation.
* **Nothing is stored in the metadata store.** A condition reads the snapshot and
  the Stage 1 evidence, and writes answers, scores and tests to a run directory.
  Every completion is kept (`<run>/llm`, keyed by the request): an interrupted run
  resumes for free, and judging or reporting again costs nothing already asked.
* **Profiled conditions do their own Stage 3.** Stored claim tests came from the
  named production reasoning and would leak names into blind conditions; each
  profiled condition therefore starts from Stage 1 evidence only, tests *its own*
  claims read-only on the target (in memory), and re-reasons the tables whose
  evidence changed — the production sequence, per condition.
* **Name-blind worlds** (`blind.py`): schemas, tables, columns, constraints,
  indexes and FKs become seeded opaque tokens (`s03.t007.c04`, no alphabetical
  hint); check, computed, default and filter definitions, view expressions,
  identities, test ids and details are rewritten; comments and the database name
  go; enum type names become `enum`; **data values are untouched**. A unit test
  renders every prompt of a blinded world and finds no original identifier; the
  integration test checks the same on `demo_pg` with real evidence.
* **Reference and subsets.** A column's reference is the owner's label (or
  `demo_pg` ground truth) when there is one, else its original names. Name-blind
  conditions count every column; named conditions only labelled ones (the names
  they read are no fair reference — reported for information only).
* **Judge**: one call per (condition, table), thinking off, blind to the condition,
  the score and the evidence; `yes` / `partial` / `no` and, where the reference
  lists roles, `role_ok`. Accuracy = (yes + ½ partial) / judged; AUROC = score vs
  `yes`; also the precision of what the machine would accept (`accept_at`).
* **Owner audit**: a seeded 20 % of the judged columns, **without** the judge's
  verdict (the owner judges independently), agreement computed by the report.
* **Native worst case** (`demo_pg`): `N-P` vs `N-P-h` with `--hide
  rpt.v_legacy_transactions` — the legacy tables with and without the view that
  names them — against the ground truth in `infra/pg-target/labels.csv`.
* **Criterion 5** (`make evaluate-cost`): per object, the latest model run (calls,
  tokens, model time from `audit.llm_call`) and the current profile (pages,
  queries, time), for a production `make reason` over all objects.

Estimate for the Phase 2 scope of `nge_small` (10 tables, 99 columns): about 20
reasoning calls per condition, ~10 more for the second round of a profiled
condition, 10 judge calls per condition — **~170–200 calls** for the five default
conditions, i.e. an overnight run at the gateway's current pace (~17 calls/h). The
run is resumable, so it can be split (B-C and B-P first: criteria 1 and 3).

Tests: 380 passed, 8 skipped in the sandbox (`test_evaluation.py`,
`integration/test_evaluation_pg.py`: five conditions end to end on `demo_pg` with a
fake model and judge, no identifier in blind prompts, no semantics written,
resumed run makes no call, judge → audit → report).

### Owner runs of §12 and §13b (2026-11-15/16) and fixes

**§12 (no LLM)** — outputs in `verification/12_*`, reviewed 2026-11-16.

| # | Expected | Observed | Verdict |
|---|---|---|---|
| 1 | `make test`: 388 passed, 0 skipped | **380 passed**, 0 skipped | the host ran the code before the 3C harness (then 372 + 8 live = 380): not a failure, but not the code §12 was written for. Outputs now start with `make stamp` (build + git hash) |
| 2 | `line_count → payroll_line_id`, `…units_sold → …`, `line_count / units → …customer_key`: `(not kept) (weak: dense integer key)` | kept: 99 % / 99 %, 96 %, 94 % | **D3b did not hold on SQL Server** → D54 below |
| 3 | `dw.sales_wide` `profiled`, or fewer than 25 columns not exact | `partial`: 20 distinct counts not exact, 5 column scans not run, 123 s | partly. SQL Server only; non-blocking under D52. Its metrics come from the sample and are labelled so |
| 4 | `12_rescore_pg`: every table `rescored`, `calls=0` | 9 rescored, 1 `needs_reasoning` (`hr.employees`, new), `calls=0` | yes |
| 5 | `12_review_pg`: `tb_trx02.f2`, `f9` not in the queue | not in the queue | yes (D49 confirmed) |

*D54 — why D3b did not hold, and the neighbour control.* D3b measured the
key's density over the range between the smallest and largest tested value.
Counts break that model: most are small (where payroll ids are dense), a few
are huge (where ids are sparse), so the density over their whole range is low
and a 99 % pass looks specific. Reproduced in the sandbox with 197 small counts
and 3 outliers against a key dense to 20,000 and sparse beyond: 98.5 % found,
`specific`, kept — the owner's pattern (a few values, the outliers, not found).
The fix asks no model of the distribution: the same inclusion statement runs on
a **neighbour** of each value (value + a seeded offset of 1–97), which is
distributed like the values but has no reason to be a key. Neighbours found
about as often as the values mean chance. The result is specific only if that
many hits are < 1 % likely at the larger of the neighbour rate and the density.
The reproduction becomes `nearby integers … 98 % … weak evidence`, not kept; a
true reference into sparse ids stays specific (1 %). The offset is not simply
+1 because a `yyyymmdd` key's next integer is usually the next day: with +1 a
real date-dimension join (100 dates) looked like chance (98 %); with offsets up
to 97 it stays specific (31 %). It reuses `inclusion_sql`, which already runs on SQL
Server, so no new T-SQL. One extra index seek per integer test that reached
the keep threshold. A failed density or control query is now written into the
test's detail instead of being swallowed. `demo_pg`'s Stage 1 results are
byte-identical (its candidates are text codes).

**§13b (LLM, 2026-11-15 21:46 → 16th)** — `B-C` 99 columns, 20 calls, 1,834 s;
`B-P` 85 columns, 19 calls, 3,220 s, `FAILED 1`. Everything answered is kept
in `evaluations/nge_small/llm` (37 answers + 3 judge calls) and is replayed
when the command is run again.

| # | Finding | Fix |
|---|---|---|
| E1 | `dbo.customer_addresses` (14 columns, not wide) failed in B-P: `output truncated at max_tokens=16000`. The call's usage: **12,437 thinking + 3,563 answer tokens**, 924 s. With thinking on, `max_tokens` covers the chain of thought *and* the answer; the same table took 5,739 thinking tokens in B-C, the evidence made the model think twice as long. Thinking per call ranged 340–12,437 over this run: not predictable from the bundle. The error's advice ("raise `llm.max_output_tokens` for wide tables") was wrong twice: the table was not wide, and at this gateway's speed (median 15 tokens/s, slowest 8.3) a 32k answer outlasts `llm.timeout_s` (1,800 s) — and a changed `max_tokens` changes every request key, so all 37 recorded answers would be asked again | **D53**: a truncated answer is asked again with at most half the columns of the chunk that truncated (14 → 7 → 4 → 2, `MAX_TRUNCATION_SPLITS = 3`); a truncated critique is asked again in halves. Answers that fit are asked exactly as before (byte-identical prompts, so recordings and caches still apply). The evaluation recorder now keeps truncated answers, so a resumed run goes straight to the split instead of paying for the same 15-minute call. The message says where the tokens went. Same code path for `make reason` |
| E2 | In B-C the **critique of `dbo.payments` failed** (no response; transport error) and nothing said so: `failed` and `notes` were empty, its 7 columns were scored with the critique "not assessed" | the summary lists `critique_failed`, the command prints `CRITIQUE FAILED n` and exits 2; each call keeps its error in `results.json`; the report lists such tables. The failed call was not recorded, so running the command again asks it again |
| E3 | Criterion 1 compared B-P over 85 columns with B-C over 99 | **D55**: criteria 1 and 2 compare two conditions on the columns both answered and both had judged, and say how many were left out |

Tests: 393 passed, 8 skipped in the sandbox (new: truncation and critique
splits, the neighbour control (counts with outliers; a date-key join stays specific), a failed query in the detail, the paired
comparison, and on `demo_pg` a B-P run whose every table truncates until split
plus a silently failing critique, re-run with no new call).

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
- [x] 3.P → approved to start 3.0 (owner, 2026-11-14, on code review; the host run is carried into the 3.0 owner steps)
- [x] 3.0 complete → approved to start 3A *(owner, 2026-11-16: every 3.0 criterion was ticked on
      the host on 2026-11-15)*
- [x] 3B verified with the configured model on the host (`OWNER_VERIFICATION.md` §6) *(owner,
      2026-11-16)*
- [x] 3A complete — **PostgreSQL** *(owner, 2026-11-16, under D52: every PostgreSQL criterion met
      on the host, D49 confirmed by §12)*. SQL Server residue, tracked, not blocking (D56):
      D54 on the counts and `dw.sales_wide` (OWNER_VERIFICATION, SQL Server track S2, S3)
- [x] 3B complete (with the configured model, above) → together with 3A, approval to start 3C
      *(owner, 2026-11-16; 3C was started on 2026-11-15)*
- [x] 3C complete → **Phase 3 done** *(owner, 2026-11-17, P6-0: accepted with criteria 1–3 met —
      B-P 78.8 % vs B-C 61.2 % (+17.6), N-P = N-C, AUROC 0.802 — and criterion 4 recorded as
      **not met**: the owner's audit agrees with the LLM judge on 86.2 % of 94 columns, the judge
      the more lenient in 9 of 13 disagreements. Consequence: the judge is triage only, and Phase
      6 calibrates on people's labels (6B). B-P's 14 unjudged columns stay noted;
      OWNER_VERIFICATION §26c F7)*

---

## Phase 4 — Retrieval, SQL generation, guarded execution · **DONE (2026-11-17)**

**Goal.** Turn a business question into correct, safe SQL over the configured
targets — **PostgreSQL first** (`demo_pg`), then SQL Server (`nge_small`), per
D52 — and return an answer.

### What D52 changes here

* Every dialect-specific piece (the guard, the generator's dialect, the executor,
  the golden set's expected SQL and results) is built and **accepted on
  PostgreSQL**, where the assistant can run it end to end. The T-SQL counterpart
  follows in the same step, with tests.
* Offline T-SQL tests (every generated statement parsed by `sqlglot`, the guard's
  adversarial suite in T-SQL) run anywhere and stay in the suite. Live SQL Server
  results are asynchronous items in OWNER_VERIFICATION and are not acceptance
  criteria — except where D52's safety refinement applies (below, "Decisions").
* P4-7 is settled: PostgreSQL is in scope, and first.

### Order of work

| Step | What | Verifiable in the sandbox | Needs the host |
|---|---|---|---|
| 4A | **Guard and executor**, PostgreSQL: `sqlglot` AST validation (single statement, `SELECT`/`WITH` only, forbidden functions, identifiers resolved against the catalog, join paths from FKs and D48 hints, `LIMIT` injection), execution in a `READ ONLY` transaction with timeout and row cap; chat/query-run migration (`0007`), every run pinning snapshot and semantic version; the adversarial suite. Then the T-SQL guard with offline tests | fully | — |
| 4B | **Retrieval**: pgvector similarity + full-text/trigram + join expansion (declared FKs first, then `inferred` hints with a passed, specific test — D48), every non-abstain meaning ranked by score with uncertainty disclosed (R2); `semantic_type` hints for generation | fully (replayed semantics, fake embeddings) | Ollama for real embeddings |
| 4C | **Planner, generator, repair, answer**: thinking off (`llm.thinking.sql_generation: false`), validate → execute → repair (≤ 2 attempts), clarification when ambiguous, the answer (narrative + table + SQL); `make ask` | functionally (replay provider, D30) | the gateway, for real answers |
| 4D | **Golden set**: questions, expected SQL and expected results; a runner and report (executable %, correct-result %, latency, tokens) | the `demo_pg` set's expected results | the run with the configured model |
| 4E | **API**: SSE chat endpoint | fully | — |
| 4F | **Data-source management** (encrypted credentials, `TargetResolver`, onboarding probe reused from 3.P, audit of connection changes) — **done** (D65) | fully | a master key in `.env` |

Host prerequisite, any time during 4A–4B: current semantics for all of
`demo_pg` (`make reason TARGET=demo_pg`, 28 objects, roughly 50 calls — an
overnight run; §11e is part of it).

Estimate for 4D on the host: ~20 questions × 2–4 calls (generate, at most two
repairs, answer), thinking off, short outputs — tens of seconds per call at the
gateway's ~15 tokens/s, so about an hour, not a night.

### Deliverables

- `query/retrieval.py` — hybrid: pgvector similarity + trigram + FK expansion.
  **Uses every non-abstain meaning, ranked by score, and discloses uncertainty
  (R2)**; `accepted` is not a filter, because in the worst case almost nothing is
  accepted before an SME has looked. `semantic_type` (Phase 3) informs generation
  (e.g. `date_int_yyyymmdd` needs a conversion). Join paths: declared FKs first,
  then model hints that are `inferred` with a passed, specific test of the claim
  (D48); `stale` and `refuted` hints never
- `query/planner.py` — entities, measures, filters, grain, joins; clarification
  decision when ambiguous
- `query/generator.py` — SQL generation in the target's dialect with compact schema context
- `query/validator.py` + `dialects/<d>/guard.py` — `SqlGuard` per dialect on a
  `sqlglot` AST (`read="postgres"` first, then `read="tsql"`)
- `query/executor.py` — read-only execution, timeouts, row caps
- `query/answerer.py` — natural-language answer from results
- `api/chat.py` — SSE streaming endpoint
- `db/models/chat.py`, chat migration (`0007`, number assigned at creation, R4)
- `tools/ask.py` CLI (`make ask`), golden-set runner (`make golden`)
- **Data source management** (see OUTLINE §24): encrypted credentials on
  `catalog.data_sources`, a `TargetResolver` that reads targets from the
  database, the onboarding probe reused from `dialects/<d>/probe.py` (3.P), per-target
  limits, and an audit table for connection changes (step 4F)

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

- [x] The `demo_pg` golden set (~20 questions) returns executable SQL for ≥ 80 % and the
      expected result for ≥ 70 %, with the configured model on the host *(the second
      threshold is new, proposed 2026-11-16: executable but wrong SQL is the costlier failure)*
      *(4D: the set has 32 questions — 21 to answer, 11 to clarify, 7 of them the owner's; 30
      with a reference; the runner and comparison are built and tested with a scripted model;
      the measurement is `make golden` on the host, OWNER_VERIFICATION §18b)*
      ***Host, D65, `deepseek-flash`, 2026-11-16: executable 100 %, correct 97 % (29/30)**; the
      one wrong answer is g07 ("overdue" = status code 2, known to no catalog) — see "Owner runs
      on D65"*
- [x] Zero statements outside `SELECT`/`WITH` reach execution, including adversarial
      prompts (enforced by test, not by inspection) — PostgreSQL **and** T-SQL guard
      (offline tests; both blocking, D52 refinement b) *(4A + D58: 78 + 46 refused statements,
      `test_sql_guard_pg.py`, `test_sql_guard_tsql.py`; sqlglot 25.30–30.30)*
- [x] Every `query_run` pins `snapshot_id` and semantic version *(4A, `test_query_pg.py`)*
- [x] Execution respects timeout and row cap; violations are recorded *(4A on PostgreSQL;
      SQL Server live: track S4)*
- [x] Repair loop bounded at 2 attempts; failures surfaced, not hidden *(4C: guard refusals,
      database errors and timeouts repaired; exhausted repairs end `failed` with every attempt
      and its problem; `test_ask_pg.py`)*
- [x] Ambiguous questions produce a clarification request instead of a guess *(4C: built — the
      prompt's rule (P4-4), a clarification without the model when nothing matches, the
      golden set's four clarify questions; to be measured with the configured model in 4D)*
      ***Host: 10 of 11 asked back (c04 `cannot_answer` counted right); o05 answered without
      asking (a policy miss whose number was the owner's); 0 of 21 clear questions
      over-clarified***
- [x] Latency and tokens recorded per run *(4C: per attempt on `chat.query_run`, per call in
      `audit.llm_call`)*
- [x] Retrieval *(proposed with 4B, a trial like the correct-result target)*: on the `demo_pg`
      question set, with real meanings and `bge-m3` on the host, every expected object is in the
      context for ≥ 90 % of the questions and joined there for ≥ 80 % (sandbox, names-only
      meanings, no vectors: 100 % and 86 %) ***Host, D65, 2026-11-17 (§16b): covered 95 %,
      connected 95 %, recall@5 95 %, MRR 0.96** — with words only: Ollama was not running, so
      this is the lower bound; vectors can only add. The miss: "order status changes per day"
      (`sales.order_events` ranked 7th of 6). `nge_small` (§16c, words only): 90 % / 90 %*
- *Asynchronous, not blocking (D52):* the `nge_small` golden questions on SQL Server

### 4A results (2026-11-16, sandbox: PostgreSQL 16 target and metadata store)

Built (D57): `query/validator.py` (the dialect-independent guard), `query/catalog.py`
(objects and join-path key classes from the store), `dialects/postgres/guard.py`,
`dialects/mssql/guard.py` (registered lazily), `query/executor.py`,
`query/guard.py`, `db/models/chat.py` + migration `0007` (`chat.query_run`),
`db/repositories/query_store.py`, `tools/query.py` (`make query`, `make query-check`),
config `query.unverified_joins`.

Verified:

* **Adversarial suites**, blocking: PostgreSQL 75 statements refused with a specific
  code (DML/DDL, `COPY … PROGRAM`, `DO`, `CALL`, `SET`/`RESET`, `LISTEN`, writable CTEs,
  `SELECT INTO`, `FOR UPDATE`, `set_config`, `query_to_xml`, `dblink`, `pg_sleep`,
  `pg_read_file`, `lo_import`, `nextval`, advisory locks, system catalogs, unknown or
  ambiguous objects and columns, unbacked joins …) and 19 realistic statements accepted;
  T-SQL 45 refused (`EXEC`, `OPENROWSET`/`OPENQUERY`/`OPENDATASOURCE`, linked servers,
  other databases, `#temp`, `SELECT INTO`, locking hints, `OPTION`, `@variables`,
  `NEXT VALUE FOR`, `WAITFOR`, `SHUTDOWN` …) and 10 accepted; row caps on both. The same
  suites pass with sqlglot 25.30, 26.17, 27.29, 28.10 and 30.30; where old parsers differ
  they refuse (safe), never accept.
* **Parser surprises the design absorbs**: `LISTEN x` parses as an alias, `SHUTDOWN` as a
  column, `EXPLAIN`/`DO`/`CALL` as commands (hence a root allowlist); `… FROM t SHUTDOWN`
  executes as an alias because the rendering is what runs; `qualify` expands `NATURAL` and
  `USING` joins and does not check tables (hence table resolution first and `NATURAL` on
  the original tree); `TOP … PERCENT` keeps its flag in different places per version.
* **On `demo_pg`** (`test_query_pg.py`): 11 realistic statements — FK chains, CTEs,
  windows, a `generate_series` date spine against a view, a materialized view,
  `percentile_cont`, a self-reference, legacy `yyyymmdd` arithmetic, two FKs to one key —
  accepted *and executed*; a D48 hint with a passed test joins, view lineage carries it to
  `rpt.v_legacy_transactions`, a `refuted` hint does not (mutation-checked: accepting
  refuted hints fails the test); a 1 s statement timeout cancels; the cap returns 5 of
  250,000 rows and says so; a `CREATE TABLE` forced past the guard meets "read-only
  transaction"; runs are recorded with snapshot and `sales.orders` semantic version 1.
* Migration `0007`: DDL identical to the model (`pg_dump` diff), downgrade and upgrade clean.

Not verified here (SQL Server track, D56): the T-SQL guard's output executed on SQL
Server, its timeout and a forced write (S4). Known limits, by design for now:
`LATERAL` / `CROSS APPLY` (top-N per group) and semi-joins are not join-checked —
the former are rejected, the latter (`EXISTS`, `IN`) allowed; mixed-case PostgreSQL
object names are rejected by the qualifier (none exist in `demo_pg` or `nge_small`).

Tests: 565 passed, 10 skipped in the sandbox (skips: 8 SQL Server live, 2 Ollama).

### 4B results (2026-11-16, sandbox)

Built (D59): `query/retrieval.py` (ranking, join expansion, column choice, budget,
rendering), `query/retrieval_store.py` (the index from the store; pgvector search),
`tools/retrieve.py` (`make retrieve`, `make retrieval-eval`), config `retrieval.*`,
question sets `infra/pg-target/retrieval_questions.csv` (22) and
`infra/sqlserver/retrieval_questions.csv` (10, a draft for the owner to correct).

**A defect in 4A found while building 4B (D58).** 4A made view lineage a join edge
and accepted joins between two children of one key (`shared_key`). Both let rows
multiply: `v_daily_sales.channel = sales.orders.channel` was a "join", and
`order_lines ⋈ payments ON order_id` pairs every payment with every line of its
order — a sum over it is silently wrong (the *fan trap*, the commonest silent error of
generated SQL). Now: lineage is an alias (a view column *is* its base column), and
every join must, besides following a join path, have **one unique side** — a key of
the catalog (PK, single-column unique constraint or unfiltered unique index), a
derived table grouped by the join column (or a single-column `DISTINCT`), or a
generated series. The refusal says what to do ("aggregate one side by the join
column first, or join through the parent"), for the repair loop. Sibling joins
stay possible after aggregation. New refusals in the suites (fan-outs on both
dialects, a view joined back to its base on a non-key); the guard suites pass on
sqlglot 25–30 again.

Verified:

* **Unit** (`test_retrieval.py`, synthetic index): tokens (abbreviations, camelCase,
  stems, "last month" is a time filter, not words); meanings and synonyms rank the
  right objects; *values find filter columns* ("web orders from Istanbul" → `channel`,
  `city`); an object with no meaning is found by its names and still gets its
  `semantic_type` hint; a two-bridge path (`products ← order_lines → orders →
  customers`); confidence raises rank, never filters (R2); vector hits add what words
  miss; uncertain meanings and personal-data columns are disclosed; truncated value
  lists say so; wide objects keep keys, join and matched columns; the budget trims the
  lowest ranked first; unconnected objects are noted; deterministic output; T-SQL and
  PostgreSQL conversion hints; **every join a context offers passes the guard**.
* **On `demo_pg`** (`test_retrieval_pg.py`; fake meanings from names, fake vectors):
  28 objects indexed; an abstained meaning is not used (the column keeps its name);
  no values for `email` and `phone` (personal), enum labels for `segment`, the
  profiled values of `channel`, date ranges, `date_int` on `legacy.tb_trx02.f3`;
  pgvector returns a stored meaning as its own nearest neighbour, only current
  versions, never another model's vectors; every join offered for three questions
  passes the guard *and runs*.
* **A names-only baseline** on the 22 `demo_pg` questions (no vectors, meanings
  derived from names, questions written by the designer of the database — a
  functional number, not accuracy): every expected object in the context 100 %,
  joined there 86 %, recall@5 91 %, MRR 0.92, 5.6 objects and ~1,800 tokens per
  context. The three not joined are exactly the joins that exist only as model hints
  (`legacy.tb_trx02.f2`, `f9`, `sales.orders.sales_rep_id`) — the sandbox has no
  model; the owner's store has the first two. Tighter cutoffs saved ~160 tokens and
  lost coverage: defaults kept until real numbers come from the host.

Not verified here: retrieval with real meanings and `bge-m3` (OWNER_VERIFICATION
§16, L5), and on `nge_small` (S6). Known limits: English only (`app.locale`);
lexical matching has no fuzzy spelling; a question naming a value that is not among
the profiled top values (a rare city) finds the column only through its meaning.

Tests: 588 passed, 10 skipped in the sandbox.

### 4C results (2026-11-16, sandbox)

Built (D60): `query/planner.py` (what the model returns, its messages),
`query/prompts/generate_sql.v1.md` and `answer.v1.md` (versioned like the semantics
prompts), `query/ask.py` (retrieve → generate → guard → execute → repair → narrate,
confidence), `query/answerer.py` (the narrative; masking; fallback), `query/golden.py`
(the golden file format), `QueryStore.record_answer`, `tools/ask.py` (`make ask`), config
`llm.thinking.answer`, `query.rows_to_model`, `query.as_of`. The golden set of `demo_pg`
(P4-1, the assistant's part): `infra/pg-target/golden.yaml`, 21 questions with reference
SQL and 4 that must be clarified, `as_of` 2026-01-15.

Verified:

* **Every path on `demo_pg`** (`test_ask_pg.py`, a scripted model): an answer (result =
  the SQL run directly, narrative, confidence); a database error (`invalid input syntax`)
  repaired in the same conversation; a timeout repaired; three refused statements
  (`pg_sleep`, `DELETE`, a system catalog) ending `failed` after 2 repairs with **nothing
  executed**; a clarification with options; "cannot answer" with its reason; a result
  with e-mail addresses that the user sees and the narrative model does not (`[hidden]`,
  D24); one `chat.query_run` per attempt, chained by `parent_id`, with model and tokens,
  and every call in `audit.llm_call`.
* **The golden set through the whole pipeline**: all 21 reference queries are accepted by
  the guard and return exactly what they return when run directly (so every golden
  answer is reachable — including the two legacy questions through the model hints), the
  four clarify questions end as clarifications, and retrieval (names only, no vectors)
  held every object each answer needs, 21 of 21.
* **A replay run of `make ask`** with the assistant as the model (D30, functional only): a
  first answer that joined order lines to payments on the order id was refused as a fan
  trap (D58), the repair request carried the guard's explanation, the corrected query
  answered g02 exactly (Adana, 3,934,224.77); with no narrative on file the plain narrative
  stood in; confidence `low (0.20)` because the sandbox has no meanings — and it said so,
  column by column.
* **T-SQL offline** (D52 b): the same pipeline with the T-SQL guard — a locking hint refused
  and repaired, `TOP 1000` added, SQL Server named in the prompt.

Not verified here: anything the configured model does — whether it clarifies when it
should and only then, how often its SQL is right, what its narratives say. That is 4D's
golden run on the host (OWNER_VERIFICATION §17 tries five questions first). Known limits:
English only; one clarification round is passed back as text (`--clarified`), the
conversation memory of the API comes with 4E; the confidence is a heuristic until Phase 6.

Tests: 604 passed, 10 skipped in the sandbox.

### 4D results (2026-11-16, sandbox)

Built (D62): `query/compare.py` (is a result the reference's answer), `query/golden_run.py`
(the runner, the summary, the report), `tools/golden.py` (`make golden`); the golden
format gains `clarified`, `identify_by`, `rank_by`. D61: `generate_sql.v2.md` (a
clarification carries Gaia's best guess), `chat.clarification` (migration `0008`),
`--clarification ID --clarified N` in `make ask`, `tools/clarifications.py` (`make
clarifications`). D63: retrieval additions found by the owner's questions.

**The owner's seven questions** (P4-1) are all ambiguous in a way that changes the numbers —
"most valuable" (by amount, orders, margin; when), "ten dates … average … higher order
counts" (which year, which average, how ranked), "performing best" (by what), "top 10 in
2024 comparing with 2025" (top by what, ranked in which year), "most returned" (returns or
units; how many), "most profitable" (realised or catalogue margin; when), "best supplier"
(by what) — so each expects a clarification (P4-4), with the answer the owner would give
(`clarified`) and its reference. They also showed three things the sandbox run found:

* **"shops"** matched nothing: the database says "stores". With no object found, Gaia asked
  back without calling the model. → D63: a short list of everyday business words and the
  words databases use for them (shop → store, client → customer, revenue → amount, vendor →
  supplier, representative → employee …), applied to the question only; number words and
  comparison words became stop words; "status" no longer stems to "statu".
* **Implied objects**: "who was the best supplier in 2025?" names neither the sales nor their
  dates. → D63: a question that ranks brings the largest table referencing its best object (the
  activity behind the ranking); a question naming a period brings the dated table a fact
  without dates references. On the names-only sandbox index the owner's seven went from 57 %
  to 86 % fully covered, the whole golden set from 90 % to 97 % (joined: 83 % → 90 %), the 22
  retrieval questions unchanged (~60 more tokens per context). The remaining miss (o06) needs
  `sales.orders` only for the clarified population, and the clarified re-ask retrieves it.
* **Ties at the cut**: in three of them the 10th and 11th rows are equal, so "the ten" has
  several right answers → `rank_by` in the comparison (D62).

Also checked: a synonym list can hurt — "per day" → "date" pulled every dated object ahead of
the one asked for, "sold" → "quantity" crowded out the employees; both removed, and the list
excludes time words on purpose.

Verified:

* **Comparison** (`test_compare.py`): columns by value, extras allowed; one identifier of
  several; numbers at the precision shown (39.03 = 39.0297, 0.03 ≠ 0.0234, 2.45 = 2.4499, 3.00 ≠
  2.99); rows as multisets, or sequences where order matters with ties interchangeable; ties at
  the cut.
* **The runner on `demo_pg`** (`test_golden_pg.py`, a scripted model erring on purpose): all 32
  references — the owner's included — pass the guard and run; right answers scored right; a
  kept "cancelled" scored wrong; an over-clarified clear question re-asked "as stated" and
  scored; a clarify question answered with a number counted as a policy miss; a wrong best
  guess scored wrong while the clarified answer is right; summary 29/30 correct, 7/8 best
  guesses; a re-run makes no model call.
* **D61 end to end** (replay, the assistant as the model): o01 asked back with three options,
  stored with the best guess (valid, not run), the default not shown; answering `1` (Gaia's
  option) answered from the stored guess **without a generation call**, linked the run,
  recorded `took_default`; `make clarifications` reported agreement 1/1 for `ranking`.
* Migration `0008`: DDL identical to the model, downgrade and upgrade clean.

Not verified here: the configured model's golden run — the Phase 4 acceptance numbers, how
often it clarifies when it should and only then, and how often its best guess is the owner's
answer (OWNER_VERIFICATION §18b).

Tests: 616 passed, 10 skipped in the sandbox.

### 4E results (2026-11-16, sandbox)

Built (D64): `api/chat.py` — `ChatService` and the endpoints (`GET /api/data-sources`,
`POST/GET /api/conversations`, `GET /api/conversations/{id}`, `POST
/api/conversations/{id}/messages` as server-sent events or JSON); `chat.conversation`,
`chat.message` (migration `0009`); `generate_sql.v3.md` (follow-up questions);
`ask(history=…, progress=…)`; config `chat.keep_rows`, `chat.history_turns`;
`llm.provider: replay` honoured by every tool.

Verified (`test_chat_api_pg.py`, a scripted model; and a real uvicorn server with the
assistant as the model through replay, `curl` as the client):

* a question streams `message`, `status` (retrieval, retrieved, generation, checking,
  running, narrating), `result` — **the table before the narrative** — then `answer`, `done`;
  the SQL is in neither (P4-2);
* a follow-up ("And in November 2025?") is sent with "Conversation so far", the earlier
  question and what was computed for it (prompt v3);
* a clarification, answered with `1` — Gaia's own option — answers from the stored best guess
  with **no generation call** and records `took_default`; with `reply_to_clarification: false`
  a new question is asked instead while a clarification is open;
* a result with e-mail addresses: in the live answer, `[hidden]` in the stored turn
  (`masked_columns`); the conversation lists every turn in order, titled by its first question;
* JSON mode (`?stream=false`); 404 for an unknown conversation or data source, 422 for an empty
  message; an internal error ends the turn as `failed` and the conversation goes on;
* the retrieval index loaded once and reused across turns (reloaded when the snapshot, the
  meanings, the hints or the tests change);
* a slow model gets keep-alive comments; found on the way: the first version waited for the next
  event up to 15 s after the work had ended — every turn 15 s slower (test: 145 s → 7.5 s).
* Migration `0009`: DDL identical to the model, downgrade and upgrade clean.

Not here: authentication (P5-1 — the API is for `127.0.0.1`); token-by-token streaming of the
narrative (the narrative arrives whole, after the table); the configured model through the API
(OWNER_VERIFICATION §19).

Tests: 619 passed, 10 skipped in the sandbox.

### 4F results (2026-11-16, sandbox)

Built (D65; OUTLINE §24, §37): `datasources/crypto.py` (AES-256-GCM, the key
`GAIA_MASTER_KEY` in `.env`, a previous key for rotation, the ciphertext bound to its data
source's id), `datasources/resolver.py` (`Settings.target(name)` falls back to the store:
config first, unchanged), `datasources/service.py` (add, probe, update, enable/disable, remove,
rotate, list), `catalog.data_sources` columns (`origin`, `status`, `connection`, `secret`,
`secret_key_id`, `probed_at`, `probe`, `tombstoned_at`), `audit.connection_change` (migration
`0010`), `tools/datasource.py` (`make datasource`, `make master-key`), `api/admin.py`
(`/api/admin/data-sources`, **off** unless `datasources.admin_api: true` — there is no login
before P5-1), config `datasources.reprobe_days` (30). `GET /api/data-sources` lists both kinds
with `usable` (verified and introspected).

The rules, each enforced by a test: the password is encrypted before it is stored and never
returned, logged, audited or put in an error (the admin API's request model hides it even in
its own `repr`); a data source is **unusable until the read-only probe passes** — the same nine
checks as `make check-target`, privileges and a write that must fail — and again after any
change or when the last pass is older than `reprobe_days`; a name is unique across config and
store (a stored row never shadows a config target); removal tombstones (the row, its snapshots
and runs stay; the secret is wiped; the name is freed as `name~<id8>`); every action is an
`audit.connection_change` listing the fields changed, never values.

Verified (`test_datasources.py`, `test_datasources_pg.py`, and the CLI by hand against the
sandbox's `demo_pg`):

* the lifecycle: `add` → `unverified`, a command with it refused with the reason and the fix
  (`make datasource ARGS="probe …"`); `probe` → `verified`, 9 of 9; then **every existing
  tool works with it with no change**: `make introspect TARGET=shop_ro` read 28 objects, a
  guarded query counted 250,000 orders;
* the same database with the superuser: the probe ends `failed` on `security.privileges`, the
  data source stays unusable;
* an update (the timeout) and a proof older than `reprobe_days` both make it `unverified`;
* rotation: a new master key, `rotate` re-encrypts every stored secret, the old key is no
  longer needed; a wrong key, a secret moved to another row, or a tampered byte fail to decrypt;
* removal: tombstoned, secret wiped, snapshots kept, the name reusable;
* the admin API answers 404 until switched on; then add, probe, list, remove over HTTP; a config
  target cannot be changed through it; no response contains the password;
* migration `0010`: DDL identical to the models for `catalog` and `audit`, downgrade and
  upgrade clean.

Also in this step, for the owner's runs (D66): `make evaluate-freeze` and frozen 3C inputs;
`GAIA_LLM_CONCURRENCY`; `make retrieval-eval` and `make golden` stop trying embeddings after the
first failure (85 s → 6 s without Ollama); `scripts/owner_runs.sh`.

Not here: SQL Server data sources added at runtime (the same code path; the probe is the 3.P
probe of each dialect — an S-item when the owner wants one); a web form (Phase 5); secrets in an
external vault (possible later behind the same `crypto` interface; not asked for).

Tests: 626 passed, 10 skipped in the sandbox.

### Owner runs on D65 (2026-11-16) — triage 2026-11-17

Outputs of OWNER_VERIFICATION §13b–§20 on build `2026-11-16 · D65 (4F)` (git `2c06491`);
item by item in OWNER_VERIFICATION §21. **No safety finding** (D52 b).

**Confirmed.** The golden run (executable 100 %, correct 97 %, 0 over-clarifications, 10 of
11 ambiguous questions asked back, 72 calls in 120 s); the guard and executor on PostgreSQL 18
(§15b); the chat API end to end (§19b); T-SQL answers (S7); D54 on SQL Server (S2: the four
counts and four `session_id → page_view_id` false positives no longer kept, date keys kept);
`dw.sales_wide` now `profiled` (S3); `demo_pg` and all of `nge_small` reasoned (`failed=0`;
D53 split the six `nge_small` tables that truncated); 3C N-C and N-P; the frozen 3C inputs
replay B-C and B-P with 0 new calls (D66).

**The model changed.** The host moved from `deepseek-v4.1-flash` to `deepseek-flash` on
2026-11-16. It writes ~190 tokens/s with thinking against ~15 before, so the "overnight"
estimates in this file are history: a full `nge_small` reasoning pass now takes about an hour.

**The one wrong golden answer (g07)** is the model's: "overdue" is `finance.invoices.status =
2`, which nothing in any catalog states; asked the same question in §17b, it read "due date
before today, every status" and said so in its narrative. An SME confirming that value meaning fixes it, and Phase 5 makes that an
acceptance test. The confidence heuristic rated this wrong answer 0.71 and a right one 0.20, a
reminder that it is not calibrated before Phase 6.

| # | Finding | Disposition |
|---|---|---|
| T1 | Two 4F tests failed on the host: they used placeholder passwords that only the sandbox's trust authentication accepts. The product was right (both data sources ended `failed`, unusable) | The delivered test file already reads the host's passwords; the owner re-runs it (§21c). Process: the build marker changes with *every* code or test change (D56) — it did not, so the stamp could not tell the two test files apart |
| T2 | `make reason` has no per-data-source lock (profiling has one, D33). §14d's second run re-reasoned 24 tables (46 calls, 8 cache hits) and printed the first run's distribution — the signature of two overlapping runs | 5.0: one reasoning/re-scoring run per data source at a time. Owner's Q1 decides whether anything else is wrong |
| T3 | The 3C recorder keys answers by the request, not by the model, so a condition answered across the model switch mixes models silently | 5.0: the report names the model(s) of each condition and marks a mixed one; judging waits for the owner's model count (§21d) and Q3 |
| T4 | B-P-nt: 7 calls refused with `http 400`; evaluation results keep only the status, not the gateway's reason ("failures recorded, not swallowed") | 5.0: keep the first 500 characters of a failed call's answer in `results.json`; then B-P-nt again |
| T5 | Gaia's choice was option 1 in 13 of 13 clarifications, and the API sends `default` and `best_guess` to the client — D61's agreement would be inflated by position, and a browser would see the choice | 5.0, before any chat screen: options shown in an order shuffled per clarification and stored; Gaia's choice kept on the server |
| T6 | `make ask --clarified '…'` without an id asked again (the id paths answer) | 5.0: pass the question it answers; the prompt says a clarification settles what it answered |
| T7 | Confidence is not correctness yet (g07 wrong at 0.71, g01 right at 0.20 while meanings were still being written) | Expected before Phase 6; Phase 5's badge shows the level and its reasons, never a percentage |
| T8 | `all_test_latest.out`, `13_reason_all.out`, `13b_*` carry no stamp; `20_tests.out` ran without the test databases | Owner: stamp every output (D56) |

### Decisions needed

Renumbered `P4-n` (formerly 11–16, which collided with Phase 3 and Phase 6).
Revised 2026-11-16 for D52.

| # | Decision | My recommendation |
|---|---|---|
| P4-1 | Golden questions: who authors ground truth *(2026-11-16: the assistant's `demo_pg` set is written; the owner's seven `demo_pg` questions added as o01–o07 — each ambiguous, so each expects a clarification, with the owner's intended answer as reference; `nge_small` questions pending)* | **`demo_pg`**: I write ~20 with expected SQL and results (checkable in the sandbox), and you add 5–10 phrased from a short business description without looking at the schema — I designed the database, so my questions alone would flatter the system. **`nge_small`**: you supply ~10–20 real questions; I write the expected SQL; runs are asynchronous |
| P4-2 | Answer style | ~~narrative + table + SQL~~ **Decided (owner, 2026-11-16): table + narrative; the SQL is not shown to users** (kept for audit, `--show-sql`). Charts in a later version |
| P4-3 | Default row limit | 1000 |
| P4-4 | Clarify vs best-guess on ambiguity | **Decided (owner, 2026-11-16): clarify, always** |
| P4-5 | Admin-supplied connections: in Phase 4 or Phase 5 | **last step of Phase 4 (4F), cuttable to Phase 5** if the schedule needs it — the query path needs only the config targets; Phase 5's UI is the first real consumer of stored credentials. *(2026-11-16: built as 4F on this recommendation, D65; the owner may still move its HTTP side — off by default — to Phase 5)* |
| P4-6 | Credential encryption | **application-level AES-GCM**, master key in `.env`; `pgcrypto` is the fallback if key rotation outside the DB proves awkward. *(2026-11-16: built this way, D65; rotation works outside the database — `pgcrypto` not needed)* |
| P4-7 | PostgreSQL in Phase 4 scope | **settled by D52**: yes, first |
| P4-8 | `app.default_target` → `demo_pg` | **Decided (owner, 2026-11-16): no** — the default stays `demo_mssql`; PostgreSQL commands name `TARGET=demo_pg` |
| P4-9 | Start Phase 4 before 3C's runs finish | **yes**: 3C decides whether the scores rank meanings well (thresholds, calibration in Phase 6); Phase 4's code ranks by score either way, and its golden set measures the end result directly |

### Gate

- [x] **Approved to start Phase 4** (under D52) — owner, 2026-11-16: "start with 4A"
- [ ] P4-1 … P4-9 answered *(settled: P4-2, P4-4, P4-8 by the owner on 2026-11-16; P4-7 by D52;
      P4-9 — 4A started before 3C finished; the correct-result target is kept as a trial.
      P4-5/6 — built on the recommendations (D65), the owner may override.
      Open: P4-1 — the owner's golden questions for `nge_small`; P4-3 (1000) in use)*
- [ ] golden questions: `demo_pg` set written and reviewed; `nge_small` questions (asynchronous)
      *(the `demo_pg` set ran on the host with the owner's seven as written; the owner's
      confirmation that the seven `clarified` texts say what he meant is the "reviewed")*
- [x] **Phase 4 complete** *(owner, 2026-11-17: Phase 5 approved after the §21 triage, with the
      retrieval trial in — 95 % / 95 %. Both thresholds met with margin, every blocking
      acceptance criterion ticked, nothing in the triage a safety finding. Carried, not
      blocking: `nge_small` golden questions (P4-1, D52 asynchronous), SQL Server S4)*

---

## Phase 5 — Sign-in, roles and need-to-know; SME review first, then chat; administration (R1; scope revised 2026-11-17) · **approved 2026-11-17 — every stage built (5.0 … 5E); the D75 host run triaged (D76, D77); host verification: §26e**

**Goal.** People of a company sign in with their directory account and get what their role
allows: an **SME** reviews and corrects Gaia's meanings for the data sources assigned to them,
in priority order; an **administrator** connects data sources and the directory and decides who
does what; **every user** asks questions and gets answers — only about data their role may see
(need-to-know), enforced in Gaia's code, never by a prompt.

Human-in-the-loop is still the most important part of the product (premise A1), so the review
screens still come before the chat screen (R1). Phase 3 supplies the ranked `review_item`
queue, the evidence ledger and the `sme` evidence kind the review writes to.

### What the owner added (2026-11-17)

1. **A sign-in screen and three roles.** *Chat user*: asks and gets answers, nothing more.
   *Administrator*: connects data sources, defines the directory connection (AD/LDAP …),
   assigns roles and authorisations; also chats, **cannot review**. *SME*: reviews at least one
   data source; also chats.
2. **Authentication through Active Directory/LDAP or similar** — the customers are companies.
   Gaia reads the users and their roles from the directory, builds the distinct list of roles
   and aligns them with the data it has reasoned about. That alignment is a guardrail: **no one
   may ask a question whose answer is not categorised for their role** (need-to-know).
3. **An administration screen** beside review and chat: data connections, the directory
   connection, users. Later: start/stop runs by hand, configure the overnight batch for database
   changes, the LLM and embedding connections.
4. Some of this may move to later phases — hence the stages and the cut line below.

### Assessment (the assistant, 2026-11-17)

**Can Phase 5 start?** Yes, once the owner ticks the Phase 4 gate (its evidence is in: 100 % /
97 %) and approves this plan with the decisions needed first (P5-1 … P5-3, P5-8 and P5-10 for 5A;
P5-11, P5-12 for the scope). Nothing in the triage is
a safety finding. Step 5.0 is the triage of the host runs (D52 c: triage comes before new
work), needs no decision, and can start at once — before the approval. 3C does not block Phase 5, as
it did not block Phase 4 (P4-9, P5-11): Phase 5's screens rank and show scores whatever their
calibration turns out to be, and Phase 6 needs Phase 5's SME labels anyway.

**The roles.** These are *application* roles — four since the owner's answers (D68): `user`,
`sme`, `admin` and `auditor`, plus the `pii_processor` permission (D69). An SME is not a global
flag but an assignment to one or more data sources. The owner's rule that an administrator
cannot review is a separation of duties, and so is "an administrator cannot audit their own
work": both are enforced — the conflicting grant is kept but not applied, and it is shown to
administrators and in the auditor's report. User, SME and admin chat; an auditor chats only if
also granted `user`. Everyone who chats is subject to need-to-know.

**Authentication.** LDAP(S) against Active Directory first, behind an `IdentityProvider`
interface, so OIDC/SAML (Entra ID, ADFS, Keycloak) can follow without touching the rest. This
matters because a company that uses only Entra ID has no LDAP to bind to (P5-1). Rules: LDAPS or
StartTLS with certificate validation, never a clear-text bind. The service account that reads
the directory has its password encrypted in the metadata store, like data-source passwords
(D65). There is also a **local bootstrap administrator**, created only from the CLI: someone has
to configure the directory connection before the directory can log anyone in, and it is the
break-glass account when the directory is down (P5-2).

**"Scan all users and their roles."** In a directory, the roles are *groups*, and a company has
hundreds or thousands of them, nested. So Gaia reads only the groups under a base DN and filter
the administrator chooses (for example an OU, or a `GAIA-` prefix), resolving nested membership.
A user's groups are read fresh at every sign-in and re-checked while a session lasts, so a
disabled account loses access without waiting for any batch. A full read — the distinct list of
roles for the admin screen — runs on demand (CLI and a button); a schedule for it comes with the
scheduler (5E / Phase 7). Only what is needed is stored: login name, display name, e-mail,
enabled, groups. That is personal data (KVKK/GDPR), so the principle is minimisation (P5-3).

**Need-to-know: the part that needs the most care.**

* *Where it is enforced.* Two places, both deterministic. (1) **The SQL guard** already resolves
  every object and column a statement touches against the catalog (D57); "and this user may read
  it" is one more check on the same resolved tree, so it holds against prompt injection and
  against any SQL the model writes. (2) **Retrieval** never puts an object, column or value the
  user may not read into the model's context, so the model cannot build on it and the schema
  does not leak through answers, options or errors.
* *What is mapped.* A role × column matrix would be unmanageable: 607 columns × N groups on
  `nge_small` alone. Instead a small layer of **data categories** (business domains — Sales,
  Finance, HR … — plus a built-in *Personal data*) sits in between. Every object has a domain
  category; a column Gaia's detectors mark as personal data (3A `semantic_type`) also carries
  *Personal data*. A user may read a column only if their roles hold **every** category on it.
  **Roles hold nothing until granted** (deny by default) (P5-5).
* *Who decides.* **Gaia proposes** each object's category from what it has reasoned — schema,
  table meaning, entity, detector types. **The SME confirms** it in the review screen, and
  re-reasoning never overwrites the confirmation (D16). **The administrator grants** categories
  to directory roles. Gaia may also *suggest* which categories a role needs (from the group's
  name and description), but a suggestion grants nothing: no model ever authorises access
  (P5-6).
* *Details that decide whether it holds.* A forbidden column may not appear anywhere in a
  statement: select list, filter, join, grouping, ordering. A filter leaks too — `COUNT(*) WHERE
  salary > 100000`. View columns take their base columns' categories through lineage (D44's
  machinery; computed columns the union of their inputs). `SELECT *` expands only to readable
  columns. Clarification options and best guesses pass the same check. Each turn of a
  conversation is checked again, because grants can change mid-conversation. A refusal is
  audited and tells the user "this needs data your role does not cover — ask your
  administrator" without naming the object. Any future answer cache carries the permission set
  in its key (OUTLINE §14).
* *Not in this phase:* row-level rules (a store manager sees only their store), aggregate-only
  rules (counts but not rows), masking per role. Those belong to Phase 7's "multi-tenant access
  control and row-level-security awareness".

**A conflict to resolve.** The former Phase 5 text had a user see "the answer, **the SQL** and
the result" and asked for the SQL to be "visible and copyable". P4-2 (owner, 2026-11-16) decided
that users get a table and a narrative and never the SQL, which is kept for audit. By D12′ the
decision wins. Below, the chat screen shows table and narrative only, and the SQL appears in an
audit view for SMEs and administrators (P5-7).

**The hard part of the review.** Today a re-reason writes a new current version of *every*
column of a table (`write_table_version`), so a reviewed column would silently lose its review.
D16 says SME decisions are never overwritten. The reasoning service must therefore carry
SME-reviewed versions forward as the current meaning (the model may read them as context but
never replaces them) and mark them `stale`, visibly, only when the column's structure changes. This is back-end work inside 5B and the
riskiest part of it.

**Operations — the cut line.** Starting and stopping runs by hand, the overnight schedule, and
the LLM and embedding connections are proposed for *after* 5A–5D (stage 5E's second half,
cuttable to Phase 7, where "scheduled incremental refresh" already is) (P5-12):

* a scheduler needs a worker process (`arq` is in the stack but unused), one-run-per-data-source
  locks (T2 — reasoning has none yet), cancellation, and the run windows of D36;
* changing the reasoning model re-reasons every table (the model is in the skip key), and
  changing the embedding model re-embeds everything and can change the vector dimension, which
  is baked into `vector(1024)` (OUTLINE §15). Neither should be a button without an impact
  preview ("28 tables, ~56 calls, ~20 min");
* what Phase 5 gets instead is a **read-only "Runs" view**: what ran, when, with what result and
  cost. `reasoning_run`, `profile_run` and `audit.llm_call` already hold all of it.

### Order of work

| Step | What | Verifiable in the sandbox | Needs the host |
|---|---|---|---|
| 5.0 ✅ | **Triage fixes** (no decision needed; built 2026-11-17, "5.0 results"): T2 one reasoning/re-scoring run per data source (advisory lock, as D33); T3 the 3C report names each condition's model(s); T4 a failed call's answer kept in evaluation results; T5 clarification options shown in a stored, shuffled order, Gaia's choice kept on the server; T6 `--clarified` carries its question, and the prompt rule "a clarification settles what it answered"; the build marker moves | fully | the T1 re-run; B-P-nt after T4 |
| 5A ✅ | **Identity and access** *(built 2026-11-17, "5A results")*. Schema `auth` (migration number at creation, R4): users, directory groups, memberships, role mappings (group → admin / sme / user), SME assignments per data source, sessions, identity-provider settings (service-account password encrypted, D65); audit of sign-ins and of every change. `IdentityProvider`: local bootstrap admin (CLI, argon2id) and LDAP/AD (`ldap3`; nested groups; paged search). Sessions: opaque token in an HttpOnly, SameSite cookie, hashed at rest, idle and absolute expiry, CSRF token; groups re-checked while a session lasts. **Every endpoint except `/health` requires a session**; role checks on the server; the data-source admin API (D65) switched on behind the admin role. **Web shell**: Vite + React + TypeScript + pnpm + i18next, `api/client.ts`, sign-in screen, navigation by role. **Admin screens v1**: directory connection (with a "test connection" probe, like the data-source probe), groups and their role mapping, users, SME assignments, data sources (a form over 4F) | offline directory (`ldap3` mock), all APIs, frontend build and component tests | a real directory: the company's test AD, or a Samba AD DC / OpenLDAP container (P5-10) |
| 5A.2 ✅ | **Microsoft Entra ID** *(built 2026-11-17, "5A.2 results", D71)* (P5-1, D68): the same provider interface over OIDC (authorization code with PKCE), roles from the token's group or app-role claims or Microsoft Graph when a user has more groups than a token carries; the administrator's role mapping unchanged. Right after 5A is verified against a real AD, so the role-source design is proven on one provider first | the OIDC flow against a local fake identity provider (signed tokens, JWKS) | an Entra tenant and an app registration (a free developer tenant will do) |
| 5B ✅ | **SME review** *(built 2026-11-17, "5B results", D73; the object's data category moved to 5C, where categories exist)*. `api/review.py`: queue per assigned data source (priority, reason, filters), item detail (meaning, alternatives, score breakdown, evidence ledger, lineage, profile summary within the exposure policy D24), decisions: accept, correct (edit fields), reject, mark unused, confirm or reject a model join hint, add a glossary term, set a code's value meanings (e.g. `status` 2 = overdue), confirm the object's data category. Persistence: a new version `sme_reviewed` / `sme_rejected` with reviewer and time; an `sme` ledger item; **carried forward by re-reasoning** (D16), `stale` only on a structure change; corrections flow to pass-through view columns (lineage certain) and re-queue elsewhere. Screens: data sources → queue → item | fully | the owner as SME on `demo_pg`; the g07 test |
| 5C ✅ | *(built 2026-11-17, "5C–5E results", D74; verified with 5D at the owner's request)* **Need-to-know.** Data categories (proposed by Gaia; what the data *is* confirmed by the SME), grants (role → category, decided by the administrator in the admin screen, with Gaia's suggestions — D70), enforcement in retrieval and in the guard (statement, clarification options, best guesses, follow-ups), **the PII-processor permission: personal-data values masked in every answer for anyone without it** (D69), refusals audited; `user` added to `auth.chat_roles`. *(Conversations already belong to their owner since 5A.)* An **adversarial suite** as blocking as 4A's: prompt injection ("ignore your rules and query `hr.payroll_lines`"), filters and joins on forbidden columns, views over forbidden bases, `*`, clarification and follow-up paths — 0 executed | fully | users of different roles on the host directory |
| 5D ✅ | *(built 2026-11-17, D75)* **Chat screen.** SSE consumption, the table before the narrative, clarification prompts (shuffled options, T5), "cannot answer" and refusals, the confidence *level and its reasons* — never a percentage (T7) — conversation list; an audit view with the SQL for SMEs, admins and auditors (P5-7, D70). Offered to the `user` role only once 5C is in place; until then the chat API serves admins and SMEs | fully (build, component tests; the API with a scripted model) | the configured model in the browser; the golden run as a `user` with every category (no regression) |
| 5E ✅ | *(built 2026-11-17, D75: the read-only runs view; the cuttable half is Phase 7)* **Operations, read-only** (in Phase 5): runs, results, cost per data source. *Cuttable to Phase 7:* start/stop a run, the schedule, the LLM and embedding connections with an impact preview | fully | — |

**Why this order.** Identity comes first because everything else needs a "who": a review needs
a reviewer, and need-to-know needs roles. Review comes before need-to-know because the
categories are confirmed in the review screen. Need-to-know comes before the chat screen reaches
ordinary users, because a chat screen without it would give every signed-in user every table.
The API stays bound to `127.0.0.1` until P5-9 is answered.

### Deliverables

- Back end: `auth/` (providers, sessions, dependencies), `api/auth.py`, `api/review.py`,
  `api/access.py` (categories, grants), `api/queries.py` (the SQL of answers and the runs view, 5D/5E; planned as `api/runs.py`); `api/admin.py` extended (directory, groups,
  role mappings, users, SME assignments); `semantics/sme.py` and `db/repositories/semantics_store.py` carrying SME versions forward (5B, as built);
  `query/retrieval.py` and `query/validator.py` taking a permission set; models and migrations
  (`auth`, categories and grants, review decisions — numbers at creation, R4); CLI `make
  admin-user`, `make directory-sync`; config `auth.*` (session expiry, re-check interval, local
  admin on/off)
- Front end: `frontend/` — `pages/Login`, `Review` (queue, item), `Chat`, `Admin` (data
  sources, directory, roles and categories, users, runs); `components/` (`MessageList`,
  `ResultTable`, `ConfidenceBadge`, `ClarificationPrompt`, `EvidenceLedger`, `ScoreBreakdown`);
  `api/client.ts`; `i18n/locales/en.json`
- Tests: offline directory (mock), role × endpoint matrix, review persistence and
  carry-forward, need-to-know adversarial suite, front-end component tests; host items in
  OWNER_VERIFICATION
- New dependencies: `ldap3`, `argon2-cffi` (back end); React, React Router, i18next, Vite, Vitest
  (front end)

### Acceptance criteria

**5.0** *(sandbox, 2026-11-17; the host run is OWNER_VERIFICATION §22a–b)*
- [x] A second `make reason` / `make rescore` on a data source while one runs is refused with the
      reason; nothing is written twice *(`test_one_writing_run_per_data_source`: refused before
      planning, 0 model calls, no run row; a dry run still works; a failing run releases the
      lock; profiling's lock is independent)*
- [x] The 3C report lists the model(s) behind each condition and flags a mixed one; a failed
      call's gateway answer is in `results.json` *(`test_report_names_the_models_behind_each_condition`,
      `test_an_evaluation_keeps_the_gateway_answer_of_a_refused_call_only`,
      `test_a_refusal_says_why`)*
- [x] Clarification options are stored in the order shown, and that order is shuffled;
      `default` and `best_guess` appear in no response to a chat client
      *(`test_clarification_options_are_shown_in_a_seeded_shuffled_order` — over 400 questions
      Gaia's choice lands on each of 4 positions 60–140 times; `test_chat_api` — no default, no
      guess, no interpretation in the SSE event; `test_golden_runner_scores_what_it_should` — `option_order` stored,
      the answer by shown number recognised as Gaia's)*

**5A — identity** *(sandbox, 2026-11-17, with an offline AD; against a real directory:
OWNER_VERIFICATION §22e)*
- [x] A directory user signs in with their directory password; a disabled or deleted account,
      or one no longer in a mapped group, cannot — at sign-in and within `auth.recheck_minutes`
      of an open session *(`test_sign_in_needs_a_gaia_role_and_says_nothing_more`,
      `test_the_directory_is_asked_again_while_a_session_lasts`: a group removed → the role
      goes at the next re-check; disabled → signed out; directory down → one more interval,
      then signed out; `test_a_revoked_grant_takes_effect_at_the_next_recheck`)*
- [x] Every endpoint except `/health` refuses without a session; a role × endpoint test matrix
      passes (a `user` cannot reach review or admin, an admin cannot review, an SME reviews only
      assigned data sources) *(`test_who_may_call_what` on `gaia.main.app`: 10 endpoints × 4
      callers; since 5B 12 endpoints, the review ones included, and
      `test_only_the_sme_of_a_data_source_reviews_it`: an SME of another data source, the
      administrator and the auditor get 403 — audited — on `demo_pg`'s queue, item and decision)*
- [x] No password (service account, local admin, data source) is stored in clear, logged,
      returned or audited (the D65 rules, extended) *(`test_the_audit_is_a_chain_and_holds_no_secret`
      — the audit refuses any detail key naming a secret; the directory secret is bound to a
      namespaced id; the sandbox's server logs of a real session hold none)*
- [x] Every sign-in, failed sign-in, role mapping, assignment and grant is in the audit
      *(append-only, hash-chained: `verify_chain` finds the row an UPDATE changed)*
- [x] The directory connection is tested from the admin screen before it is saved (the probe
      idea of 3.P); a clear-text bind is refused *(the probe — transport, bind, users, roles — and
      `test_clear_text_ldap_is_refused`; a directory is unusable until its probe passes, and
      again after any change)*
- [x] *(added with D68)* Exactly one local account; its password set by the CLI or with a one-time
      code; recovery by the CLI only; locked after 5 failures *(`test_the_one_local_account`: a
      second local row violates a unique index)*
- [x] *(added with D70)* An administrator cannot hold `sme` or `auditor`: kept, not applied,
      shown *(`test_roles_permissions_and_separation_of_duties`)*; the auditor's reports and
      CSV *(`test_the_administrator_grants_and_the_auditor_reads`)*
- [x] *(web)* sign-in, navigation by role, the admin and audit screens; `pnpm build` succeeds
      *(vitest: 14; the dev server drove a real sign-in, a data source added and probed 9/9)*

**5A.2 — Entra ID** *(sandbox, 2026-11-17, with an offline Microsoft; against a real tenant:
OWNER_VERIFICATION §23c)*
- [x] A person signs in at Microsoft (authorization code + PKCE) and arrives signed in to Gaia
      with the Gaia roles of their Entra groups or attribute; Gaia never receives a password
      *(`test_signing_in_with_microsoft`, `test_a_sign_in_gives_the_person_and_their_groups`:
      nested membership, non-security and unprefixed groups left out)*
- [x] The ID token is refused when its signature, issuer, audience, expiry, nonce or tenant is
      wrong, or when it is signed with an unpublished key; rotated keys are fetched again
      *(`test_the_id_token_is_checked_thoroughly`, `test_rotated_keys_are_fetched_again`)*
- [x] A sign-in is single use, expires in 10 minutes and completes only in the browser that
      started it; Microsoft's refusals, people without a Gaia role and disabled accounts are
      refused with the same public answer and the real reason in the audit
      *(`test_what_is_refused`)*
- [x] An open session follows Entra: removed from the group or disabled → signed out at the next
      re-check *(`test_an_open_session_follows_microsoft`)*
- [x] The probe and the sync work through Microsoft Graph with the app's credentials; a missing
      Graph permission is named *(`test_the_probe`, `test_sync_reads_everyone_and_the_distinct_roles_across_pages`)*
- [x] The web UI: "Sign in with Microsoft", the failure coming back, the Entra form *(vitest 17)*

**5B — review** (the former Phase 5 criteria) *(sandbox, 2026-11-17, `demo_pg` profiled for
real and reasoned by the tests' scripted model; the owner as SME: OWNER_VERIFICATION §24c–d)*
- [x] An SME works through a data source's queue in priority order; each decision is stored as
      `sme_reviewed` / `sme_rejected` with reviewer and timestamp *(`test_the_sme_review_from_queue_to_question`:
      the queue sorted by priority with reasons, the decision signed, score 1.0, the item closed,
      the next item offered; `test_reject_unused_views_hints_tables_and_personal_data`: reject
      needs a note and scores 0.0, "not used" leaves retrieval, edits only with "correct";
      over HTTP the decision is signed with the session's login, whatever the body says)*
- [x] SME decisions enter the ledger as `sme` evidence and are never overwritten by
      re-reasoning (D16); a later structure change marks them `stale`, visibly *(the same test:
      a forced re-reasoning keeps the SME version current — same row — while the other columns
      get new versions; a changed column hash turns it `stale`, still signed, out of retrieval,
      listed under "stale decisions" — also after the machine's new version — and a new decision
      clears it; the table's purpose and grain likewise)*
- [x] Corrections propagate where lineage is certain (view pass-through columns) and are
      re-queued elsewhere rather than silently copied *(`rpt.v_legacy_transactions.customer_code`
      takes `legacy.tb_trx02.f2`'s correction at once and cannot be reviewed itself; `quantity`
      takes `f5`'s rejection and leaves retrieval; `transaction_date`, computed from `f3`, gets
      the reason `input_reviewed`)*
- [ ] **g07 becomes right**: after an SME states what `finance.invoices.status` values mean, the
      golden run answers g07 correctly with no change to code or prompts *(the mechanism is
      verified — after the decision the SQL generator's context for g07 holds `'2' = overdue` and
      the definition of "overdue invoice"; whether the model then answers right is the host's
      run, §24d)*

**5C — need-to-know** *(sandbox, 2026-11-17: `test_need_to_know_pg.py` on `demo_pg`; with real
people from a real directory: OWNER_VERIFICATION §25)*
- [x] For a user without a category, no statement that references any of its columns
      *anywhere* is executed — the adversarial suite, blocking, 0 executed (like 4A's)
      *(`test_the_guard_and_the_context`: 33 statements — 14 forbidden, 10 misusing personal
      data, 2 with `*`, 7 allowed: forbidden objects in the select
      list, a join, `EXISTS`, a scalar subquery, a CTE, a `UNION`, a derived table, `LATERAL`,
      quoted and upper-case names, views over forbidden bases, `*`, and personal data
      filtered, grouped, sorted, computed or probed through `IN`/`EXISTS` — each with its
      expected verdict; `test_nothing_forbidden_is_ever_executed`: a model that obeys every
      injection, through the chat: 0 of them reached the database, a `*` repaired into a
      forbidden query refused too, a clarification's forbidden best guess never run, a
      follow-up and a grant withdrawn between two turns checked again)*
- [x] A user's retrieval context holds no object, column or value outside their categories
      *(the same test: four questions aimed at HR, Finance and Legacy data render no name of
      theirs; personal data is marked and shows no value; glossary definitions naming a
      forbidden object are left out; vector hits outside the index are dropped)*
- [x] Every object has a proposed category; SME-confirmed categories survive re-reasoning;
      a role holds no category until granted *(Gaia proposes for every object a category's
      name or keywords match — all 28 on `demo_pg` with categories from the schemas; a tie or
      no match proposes nothing and the object is readable by nobody, shown as such to the
      administrator. Placements live in `auth.object_category`, keyed by the catalog's table
      id — no reasoning code writes them — and Gaia's next proposal keeps a confirmed one
      (`test_the_sme_confirms_and_the_administrator_decides`); deny by default:
      `test_the_guard_and_the_context` removes a placement and the object becomes
      unreadable)*
- [x] A refusal is audited and explained to the user without naming the forbidden object
      *(`access_refused` in the hash-chained audit with the code and what it reached for; the
      reply says "This needs data your role does not cover…"; an option, question, reason or
      narrative repeating a forbidden name the user typed is dropped or replaced)*

**5D — chat** *(sandbox: component tests, HTTP tests and a run of the real processes — uvicorn
and a scripted model over HTTP; a browser: OWNER_VERIFICATION §25)*
- [ ] A question answered in the browser against each configured target; streaming renders
      progressively (the table before the narrative); no full-page reloads *(built: the
      `result` event is rendered before the `answer` event — vitest — and the real server
      streams the events in that order — `curl` against uvicorn; a browser on the host)*
- [x] The result table scrolls and formats numbers and dates; the SQL is not shown to the `user`
      role (P4-2) and is visible and copyable in the SME/admin audit view *(vitest: numbers
      grouped, dates in the locale, "Show the SQL" only for SMEs/admins; `/queries`: copy
      button; `test_the_sql_of_answers_is_for_smes_admins_and_auditors` and the role ×
      endpoint matrix — a plain user gets 403)*
- [x] The confidence badge shows the level **and** its reasons on expand (no percentage until
      Phase 6) *(vitest: "Confidence: high", its reasons on a click, no score anywhere)*
- [ ] The golden run through the API as a `user` holding every category gives §18b's numbers
      *(`make golden --as LOGIN` answers exactly as the chat does; in the sandbox the prompts
      of all 32 golden questions for a person holding every category and the PII-processor
      permission are byte-identical to the operator's — the numbers are the host's run)*

**All stages**
- [x] No hard-coded English strings outside locale files *(the web UI: every key the code asks
      for is in `en.json`, dynamic families checked value by value — vitest; the API's own
      messages, e.g. a refusal's reason, are English like every other API message, D14)*
- [x] `pnpm build` succeeds; dev server on `127.0.0.1:5173` *(build: 34 tests, then the
      bundle)*

### 5.0 results (2026-11-17, sandbox)

The triage of the D65 host runs (Phase 4, "Owner runs on D65"; D67), each with a test:

* **T2 — one writing run per data source.** `gaia.db.locks` generalises profiling's advisory
  lock (D33; profiling's key unchanged). `run_reasoning` takes a `reasoning` lock **before it
  plans**, so a second `make reason` or `make rescore` is refused with
  `REFUSED: another reasoning or re-scoring run holds '…'`; a dry run takes none; a failing
  run releases it.
* **T3 — models in 3C.** Every evaluation call records the model that answered (`answered_by`,
  from the response); the report gets a **Models** section and flags a condition answered by
  two ids (a gateway's `provider/` prefix counts as one id). Old `results.json` files say
  "not recorded" — running the condition again replays every answer for free and records them.
* **T4 — refusals say why.** A 4xx from the gateway carries its reason in the error message
  (`http 400: <the gateway's words>`, one line, ≤ 200 characters), for `make reason`, `make
  ask` and 3C alike; evaluation results keep the gateway's answer of a refused call (never a
  model answer).
* **T5 — clarification options.** Shown in an order seeded by the question and its options
  (reproducible, independent of the model's preference); `chat.clarification.option_order`
  (migration `0011`) keeps the model's index of each shown option; answering "2" means the
  second *shown* option; `answer_dict` leaves Gaia's choice out for clients (its `default`, the
  best guess's interpretation and assumptions); `make clarifications` reports how often the
  first shown option is chosen — a primacy check for D61.
* **T6 — `--clarified` without an id** answers the latest open clarification of that question
  (its question now travels with the answer); `generate_sql.v3` gained one sentence: an earlier
  clarification settles what it answered. The prompt's version changed, so the next golden run
  asks the model afresh (~2 min on the new provider).
* **Also:** `infra/pg-target/readonly_role.sql` wrote the password as an identifier
  (`:"ro_password"`), so a fresh `make pg-target-seed` failed at its last step — found while
  rebuilding the sandbox; fixed, with a test that the template uses only what `seed.py`
  substitutes. A test imports every command-line tool (a syntax error in one that no test
  imported was caught by lint during this step).

### 5A results (2026-11-17, sandbox)

Built (D68, D70 in part): `gaia.auth` — `passwords` (argon2id; tokens and codes stored as
SHA-256), `audit` (the hash-chained `audit.auth_event`), `privileges` (grants → what a person
may do; separation of duties), `ldap` (AD/LDAP(S): sign-in by the person's own bind, roles from
groups with nesting — client-side or AD's in-chain rule — an attribute or the OU, sync, the
probe), `service` (sign-in, sessions, the local account, directories, grants), `deps`
(FastAPI: session cookie, CSRF, role checks, audited 403s). Schema `auth` (migration `0012`:
`directory`, `account`, `directory_role`, `account_role`, `access_grant`, `session`) and
`audit.auth_event`. API: `/api/auth/*` (options, login, logout, me, setup), `/api/admin/*`
(data sources — now behind the admin role instead of `datasources.admin_api` — directories,
roles, accounts, grants, the local account), `/api/audit/*` (summary, access, PII processors,
grants, events, chain, connection changes; CSV). Every other endpoint needs a session; chat
endpoints a role in `auth.chat_roles` (admin, sme until 5C); conversations belong to their
owner (someone else's answers 404); clarifications record who was asked. CLI: `make
admin-user`, `make auth` (directories, grants, accounts, `audit-check`); `make datasource
ARGS=rotate-key` also re-encrypts directory secrets. Config `auth.*`. **Web UI**
(`frontend/`): sign-in (directories, the local account, its first-time setup code), navigation
by role, admin screens (data sources, directories with "test connection" and "read users and
roles", roles & grants, accounts) and the auditor's pages with CSV; every string in
`locales/en.json` (a test checks every key).

Verified: 26 new Python tests for 5A (49 in the whole build: 626 → 675) — the offline AD (`ldap3`'s mock: nesting through a group
outside the role base, a cycle, a disabled account, `DOMAIN\user` and UPN logins, filter
characters escaped, the empty-password anonymous bind refused before the directory is asked,
clear-text URLs refused), the service on the metadata store (roles, grants, conflicts, session
expiry idle and absolute, CSRF, re-checks against a directory that changes or goes down, the one
local account, lockout, the chain broken by an UPDATE and found), and the HTTP layer on the real
`gaia.main.app` (role × endpoint matrix, cookie flags, CSRF, ownership, the admin granting and
the auditor reading, the setup code). Migration `0012`: upgrade, downgrade, upgrade; DDL identical
to the models. A manual run of the real processes (uvicorn + Vite, `curl` through the proxy):
sign-in as the local account, the cookies (`HttpOnly; SameSite=strict`), a change refused
without the CSRF header, `pg_copy` added and probed 9/9 with the administrator as the actor, the
auditor's page refused to the administrator, no secret in any log, the chain intact.

Not verified here: a real directory (OWNER_VERIFICATION §22e: the company's test AD or a Samba AD
container — the script is written but could not be run without Docker), AD's in-chain rule, the
UI in a real browser (component tests and a proxied HTTP run only). Known limits: one directory
kind (Entra ID is 5A.2); group membership through `posixGroup`/`memberUid` (OpenLDAP's older
style) is not read — `groupOfNames`/`groupOfUniqueNames` and AD groups are; the access matrix
is computed per account on each request, fine for hundreds of accounts, to be revisited for
tens of thousands.

### 5A on the host (2026-11-17, build D68)

OWNER_VERIFICATION §22a–b: migrations `0011`, `0012`; **685 passed, 0 skipped** — T1's two 4F
tests included, with the host's real passwords (T1 closed); the web UI 14 tests and its build.
5.0 on the host: the clarification options in a shuffled order (Gaia's choice shown 4th),
`--clarified` answering the open clarification (T6). **The golden run: 100 % executable, 90 %
correct** (27/30; target 70 %), down from 97 %: o06 answered without asking (the previous run's
slip was o05 — one per run, another question each time) and o02 wrong after its clarification.
One run per build cannot tell noise from the new prompt sentence (T6); two fresh runs measure it
(§23b). 5A against a real directory: §22e, waiting.

### 5A.2 results (2026-11-17, sandbox)

Built (D71): `gaia.auth.entra` — OpenID Connect sign-in (authorization code with PKCE; the ID
token validated against the tenant's published keys, re-fetched once for an unknown key id:
signature RS256, issuer, audience, expiry with 2 minutes' tolerance, nonce, tenant), Microsoft
Graph with the app's credentials for roles (transitive groups by name prefix, security groups
only by default, or a user attribute), the re-check, the sync and the probe. Service:
`oidc_start` / `oidc_finish`, sharing `_admit` with the password path (the same rules once a
directory vouches for someone) and `_open_session`. `auth.pending_login` (migration `0013`): the
state and a browser-binding token hashed, the PKCE verifier encrypted with the master key, single
use, `auth.oidc_minutes` (10). API: `GET /api/auth/oidc/start?directory=…` (302 to Microsoft; the
binding cookie `gaia_oidc`, HttpOnly, SameSite=Lax, path `/api/auth/oidc`) and `GET
/api/auth/oidc/callback` — which answers with a page that moves on by itself, so the
SameSite=Strict session cookie is sent with the first page (it would not be after a redirect chain
begun at Microsoft), with `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. A password
never goes to an Entra directory. Web UI: "Sign in with Microsoft", the refused return
(`?signin=failed`), the Entra form in Directories. `infra/auth/entra-example.json`.

Verified: 13 new tests with an offline Microsoft (`tests/unit/_fake_entra.py`: discovery, keys
and their rotation, the token endpoint enforcing PKCE and single-use codes, Graph with paging) —
the provider (a sign-in's groups with nesting and exclusions; seven ways to forge or misdirect a
token; a code reused or redeemed with another verifier; a wrong secret; lookup by object id; sync
across pages; roles from an attribute; the probe, including a missing Graph permission; settings
validation — no http outside localhost, the callback path, no open redirect after sign-in, no
quote in the OData filter), and over HTTP on `gaia.main.app` (the round trip; replay, another
browser, expiry, Microsoft's refusal, no Gaia role, disabled; the audit's reasons and actors; an
open session ended when Entra changes). Migration `0013`: down and up; DDL identical to the
models. Web: 17 tests, the build.

Not verified here: a real tenant (OWNER_VERIFICATION §23c), MFA and conditional access (they
happen at Microsoft, before Gaia is involved), a real browser's cookie handling of the
callback. Known limits: a client secret, not a certificate (possible later behind the same
settings); Entra **app roles** are not a role source yet (groups and attributes are); one
callback address per app registration — serving Gaia under another host name means adding it
there.

### Owner runs on D71 (2026-11-17) — triage

OWNER_VERIFICATION §23a–b: migration `0013`; **698 passed, 0 skipped**; the web UI 17 tests and
its build — as expected. The golden run's spread: two fresh runs gave **90 % and 90 %**, like
D68's — three runs of two builds at 90 %, none near 97 %, so not noise. Which questions missed
says where: g07 in every run (known — 5B's test); **answers after a clarification that were
still wrong** — o02 (twice), o03, o05 — which D65's run never had; and one clarify question per
run answered without asking (o06, o03), as on D65 (o05). Between D65 and D68 the only change to
what the model receives was T6's prompt sentence, which bears on exactly those re-asked answers:
withdrawn (D72), the prompt is D65's again byte for byte, and two fresh runs on the new build
settle it (§24b). 5A against a real directory (§22e) and Entra ID against a real tenant (§23c):
waiting, as planned.

### 5B results (2026-11-17, sandbox)

Built (D73): `gaia.semantics.sme.ReviewService` — the data sources an SME reviews with their
queue sizes; the queue (open, decided, stale; by reason and table; priority order); **one
column, everything Gaia knows** (P5-4, D69): a few summary sentences first (what Gaia reads it
as, how sure and from how much evidence, the critique, the tests, personal data, what it is
unsure of — as keys the UI words from its locale file), then the facts from the snapshot, the
meaning with alternatives, assumptions and unknowns, the score breakdown, the evidence ledger,
the profile (values as shapes for an SME who is not a PII processor), the tests of Gaia's
claims, join hints, lineage both ways, the version history and the questions that used the
column (approximate until 5C records the columns a query read); the table's own meaning with
its columns; every table with what waits in it. Decisions on columns, tables and model join hints, and a glossary per data source
(add, edit, withdraw — kept, audited). `SemanticsStore.write_table_version` carries SME
versions forward (D16) and turns them `stale` on a structure change; `mark_stale` leaves an SME
decision alone unless its own column changed. Retrieval shows value meanings and definitions
(`IndexedColumn.meanings`, `RetrievalIndex.glossary`, `SchemaContext.definitions`); the chat's
index cache notices decisions and glossary changes. Migration `0014`. API `/api/review/*` (an
SME of that data source only; refusals audited; decisions signed with the session's login).
**Web UI:** "Review" for SMEs — my data sources → the queue (tabs to do / decided / stale,
filters, paging), or every table and view (queued or not) → the item (the summary beside the decision panel: accept, correct — with an
editor for the values' meanings, pre-filled with the values seen in the data — reject with a
note, not used; the outcome, with what travelled to views and what was queued, and "next"), the
table page, the glossary; the sections of the dive-in fold open. The auditor's Events list
review decisions and glossary changes.

Also (D72): the SQL prompt and the re-ask block are build D65's again, byte for byte — checked
by rendering all 32 golden questions with the D65 code and this build on the same store, and
pinned by a test of the system prompt's hash.

Verified: 692 passed, 10 skipped (+4: two service tests on `demo_pg` — the whole path from queue
to the SQL generator's context, carry-forward, staleness, lineage, join hints, table meaning,
personal data — one HTTP test on `gaia.main.app`, the D72 pin; the role × endpoint matrix
gained the review endpoints). Migration `0014`: down and up; DDL identical to the models. Web:
25 tests (+8: what a decision sends — only the changed fields, meanings only when given, the
values pre-filled for a code or a column of few values — the queue, the item page recording a
correction with a code's meaning, a view column that cannot be reviewed itself, the table list,
the navigation), every locale key present, the build.

Not verified here: a real SME's session in a browser (component tests and HTTP tests only; the
sandbox has no directory process to sign an SME in through a real server), the g07 golden
question with the configured model (§24d), and the review of a large data source's queue for
speed (`demo_pg` has a few hundred columns). Known limits: the "questions that used it" list
matches the column's name in the executed SQL (5C records what the guard read); a join the SME
knows but the model never proposed cannot be added yet (only proposals are reviewed); table
decisions are accept or correct (a table is not rejected — its columns are).

### 5C–5E results (2026-11-17, sandbox)

Built together at the owner's request (D74, D75), so that Phase 5 is complete for one combined
host verification with a real AD/LDAP and Entra ID (OWNER_VERIFICATION §25).

**5C — need-to-know** (`gaia.access`). *Categories*: `auth.category` (name, description,
keywords), `auth.object_category` (one current category per object: proposed by Gaia,
confirmed by an SME, or set by an administrator; migration `0015`), grants with privilege
`category`. *Gaia's proposal* is deterministic: the object's schema, name and meaning against
each category's name and keywords; a view that reads one category's objects gets it; a tie
proposes nothing. "Create categories from the schemas" gives a first set in one click.
*The policy* (`access.policy`): per person and data source, the readable columns — a table's
if its category is held; a view's if its own category is held **and** its columns' bases are
readable (pass-through: the base column; computed: every input; unknown lineage: every object
its definition reads) — and the personal-data columns (detectors, the semantic type, or an
SME's statement, which wins; a view column inherits through lineage). *Enforcement*:
retrieval runs on an index restricted to that (`restrict_index`: objects, columns, values,
joins, glossary, vector hits); the guard on the restricted catalog (`AccessGuard`): a
reference outside it fails to resolve wherever it is, `*` is refused (the database would
expand it), and only then is the full catalog asked whether the reference exists — if it
does, the turn ends `refused` (never repaired, never run, audited as `access_refused` with
what it reached for). Personal data for a non-processor: shown (masked, live and stored),
counted, or tested for `IS [NOT] NULL` — anything else is refused (`personal_data`); a PII
processor is not restricted. The chat: every turn computes the asker's policy afresh; the data
sources listed are those they can read something in; `user` joined `auth.chat_roles`. Admin:
`/api/admin/categories`, objects and their categories, proposals, suggestions of categories
for directory roles (names and descriptions against keywords — a suggestion grants nothing);
the Roles & grants page grants categories. SME: the table page confirms the object's category;
"personal data: yes / no / as the detectors say" on a column. Auditor: data categories (objects
and who may read each, through which grant), refused questions. CLI: `make access`, `make auth
ARGS="grant category …"`, and `--as LOGIN` on `make ask` and `make golden` — the operator's
command line otherwise keeps the full policy, so its prompts stay D65's (D72).

**5D — the chat screen** (`/chat`): data source and conversations on the left; the turn streams
over server-sent events read from `fetch` (the stage, the table as soon as it exists, then the
narrative); a clarification's options as buttons (or the user's own words, or "ask something
new instead"); "cannot answer", refusals and failures in plain words; the confidence as a level
with its reasons, never a number (T7); interpretation, assumptions and unconfirmed meanings
under "How I read the question"; masked cells as dots; numbers and dates formatted; a
conversation reopened shows its stored turns (rows kept, personal data masked). **The SQL**:
`/queries` — the attempts, the guard's verdict, the SQL with a copy button — for SMEs (their
data sources), administrators and auditors; "Show the SQL" under an answer for SMEs and
administrators; `chat.query_run.asked_by` records who asked. **5E — runs** (`/runs`,
administrators and auditors): reasoning and profiling runs per data source, model calls,
tokens and cost, questions asked — read-only.

Verified: **698 passed, 10 skipped** (+6: the adversarial suite's three tests, the categories
over HTTP, the SQL view's scoping, `make access` importing; the review API test gained the SME's category and personal
statement and the runs view; the chat API tests now run as a person holding every category).
Migration `0015`: down and up; DDL identical to the models. Web: **34 tests** (+9: the event
stream parsed as it arrives, the table before the narrative, formatting, the confidence, masked
cells and the SQL link, a refusal, the chat screen through a clarification, a person with no
data, the categories page), every locale key, the build. A run of the real processes (uvicorn
with a scripted model behind an OpenAI-compatible HTTP stub, sessions for two directory
accounts): the SSE stream in order, a forbidden query refused, an e-mail masked for one person
and shown to the PII processor, `/api/queries` refused to both. The prompts of all 32 golden
questions for a person holding every category and the PII-processor permission are
byte-identical to the operator's.

Not verified here: a browser (no browser in the sandbox), people from a real directory, the
configured model's answers under need-to-know (`make golden --as LOGIN`), large catalogs (the
policy is computed per turn: fine for hundreds of objects, to be cached for tens of
thousands). Known limits: one category per object (a table mixing HR and Sales data needs a
view per audience, or Phase 7's column-level rules); no row-level rules; the proposal is
keyword matching — good on schemas named after domains, weak on `dbo` — which is why every
placement is shown and confirmed by people; a forbidden name the user types is not echoed, but
"cannot answer" still tells them the question could not be answered.

### Owner runs on D75 (2026-11-17) — triage, and build D77

OWNER_VERIFICATION §26 has the details. The files (all stamped D75, most named after §22–§24)
prove: **708 passed, 0 skipped** on the host as expected; the web UI's 34 tests and a
bundle byte-identical to the sandbox's; **5A against a real AD** up to the grants — LDAPS with a
private CA, the service bind, 5 users, 3 groups, nesting through Team-HR, exactly §22e's
expectations. Then the run stopped at the directory, so 5B–5E were not verified on the host:

* **The grants failed** because §22e's block ran before migration `0015` (F1) and were never
  repeated, so no directory person had a Gaia role.
* **The directory stopped answering**: the Samba domain was provisioned again, which makes a new
  self-signed CA under the *same name*; `acme-ca.pem` held the old one → OpenSSL's *certificate
  signature failure*. Reproduced exactly in the sandbox. Gaia's test had said `transport PASS`
  without a handshake, and the failed test turned the directory `failed`, which took it off the
  sign-in page: only `gaia-admin` was left, so no SME could review (the owner's "review box").
* **Categories** proposed related tables apart on `nge_small` (`inventory` Product,
  `inventory_movements` Sales): word matching only, with categories made from `demo_pg`'s schemas.
* **Runs**: the data-source choice was built from the filtered totals. **Chat** for `gaia-admin`
  was a one-sentence dead end (the local account cannot hold categories, D68).
* Found while reproducing: **need-to-know refusals lost to repairs** (a forbidden reference beside
  any other mistake was repaired, ended `failed` and went unaudited — nothing ran, but the refusal
  and its audit were wrong); **profiling refused runtime data sources**; the 3C report hid B-P's
  14 unjudged columns.
* The golden runs: `24_golden_a/b` were replays of D73's runs (97 %, 90 %); `24_golden_sme` — fresh,
  before any SME decision — 93 %. The D72 prompt's spread is ±4 points; D72 stands.
* 3C: criteria 1–3 met (B-P +17.6 points over B-C, N-P = N-C, AUROC 0.802); the owner's audit
  agrees with the judge on **86.2 %** of 94 columns — criterion 4 not met, the judge the more lenient
  in 9 of 13 disagreements (P6-0).

**Build D77** (D76, D77; sandbox): every finding above fixed with a test, each test failing on
D75's code first. The directory test performs the TLS handshake and explains a refusal
(`gaia.auth.tls`); a failing test of settings that passed before keeps the directory at sign-in;
`ca_file` no longer depends on the working directory, and `ca_pem` keeps the CA in the settings;
the Samba script writes its CA, and gained `start`, `check`, `status` and finn; `acme-samba.json`
is delivered. `/health`, the API's start-up and every command-line tool say when a migration is
missing. `AccessGuard` decides a forbidden reference before a mistake (the adversarial suite +7
statements and a view read in part). Category proposals v2 (`gaia.access.proposal`, migration
`0016`). Profiling resolves runtime data sources. The UI: the runs and queries filters, the chat
and home of someone with nothing to ask, an error boundary, *last test failed* and the CA field on
*Directories*, the basis of each proposal and "only those to check" on *Data categories*.

Verified in the sandbox with the real processes — a real LDAPS directory (the Samba script's
people and groups, served by `ldaptor` with a Samba-shaped certificate chain), uvicorn, Vite, a
headless Chromium and a scripted OpenAI-compatible model: the owner's error reproduced and then
explained by the test, the directory kept at sign-in, every role signed in through the directory,
akaya's review decision on `finance.invoices.status` in the browser, finn's chat (the table
first, a clarification, e-mails masked, refusals now *Not allowed* and in the auditor's *Refused
questions*), the runs filter, the categories page on a `dbo`-shaped database. Tests: **714
passed, 10 skipped** (+16), web **42** (+8); migration `0016` down and up.

### Owner runs on D77 (2026-11-17) — triage, and build D80

OWNER_VERIFICATION §27. The run of §26e went through (after its migration first, as asked):
**724 passed, 0 skipped**; the web UI's 42 tests; `/health` with the schema revision. **5A on a
real AD is complete**: the CA file matched the running domain, the test's transport line named
the certificate and the CA, six users and three roles were read, the grants were made, and every
role worked in the browser ("everything worked as expected in the web UI"). **5A.2 on a real
tenant works** with the client secret's Value: all six checks, *Sign in with Microsoft* in the
browser. On a real tenant, though, **GAIA-Mail — a Microsoft 365 group — counted as a role**
(the Entra admin center makes those security-enabled too; only `groupTypes` "Unified" tells them
apart), and **GAIA-Finance was left out** as not a security group, without the probe saying so.
Categories on both databases: the owner confirmed every `demo_pg` object and 24 of `nge_small`'s,
which shows where v2 still erred (a schema outweighed by a name, a dimension not recognised as
its base table's subject, a line outvoted by its references, a lookup table pulled by everything
referencing it). akaya decided six review items — not `finance.invoices.status`, so g07 is still
untested. Two fresh golden runs — 90 % and, as dyilmaz, 87 % — join the D72 prompt's spread.
**The chat broke in the owner's browser**: "destroy is not a function" — an effect returned
`scrollIntoView()`'s value, which newer browsers make a Promise, and React took it for a clean-up
(reproduced exactly by giving the sandbox's Chromium that behaviour). And the owner's uploads were
refused by a platform's PII check: the outputs carry his Microsoft account, tenant, OS user name
and the test directory's e-mail addresses.

**Build D80** (D78, D79, D80): the chat's effects return nothing (and a test keeps it so); Entra's
"security groups only" excludes Microsoft 365 groups and the probe names every group it left out,
with why; **D78 built** (an account is the directory's object: a changed stable id refuses the
sign-in until an administrator re-links or starts the account anew; migration `0017`); proposals
v2.1; `make share` (outputs with personal data replaced consistently, the key kept by the owner);
and **Phase 6A** — the evaluation store and `make quality` (D80; migration `0018`). Verified in
the sandbox with the real processes: the chat opened in a browser whose `scrollIntoView` returns
a Promise; finn's directory object changed on the LDAPS server — sign-in refused, the Accounts
banner, re-linked in the browser, signed in again.

### Owner runs on D80 (2026-11-17) — triage, and build D83

OWNER_VERIFICATION §28 (the outputs came through `make share`, as intended). **735 passed, 0
skipped**; migrations `0017`, `0018`; the web UI's 46 tests. On the real tenant the Microsoft
365 group is left out (D79 works); GAIA-Finance does not exist there under that name. Categories
v2.1 gave every case it targeted; the owner confirmed six more. **g07 right in all four fresh D80
runs — the 5B criterion met by the owner's SME decision alone.** The quality report on real runs:
D80 pooled 96 % (89–98 %), o06 wrong in every run, D80 "passes" against D77 — crediting the
owner's decision to the build, which showed that a run must record the meanings it answered with.
The owner found "Show the SQL" ending in "no such query": an SME on a data source they do not
review (P5-7) was offered the link. D78 was exercised (finn dropped and recreated).

**Build D83** (D81–D83): the SQL link only where the person may see the SQL; each run's meanings
fingerprint and each item's SQL (migration `0019`); Phase 6's replayed net (6E), labels from
people and the blind share (6B), the *Evaluation* page (6A part 2).

### Decisions needed

*P5-1 and P5-2 of the former plan are superseded: the owner's directive settles identity (the
company directory), and review granularity becomes P5-4.*

| # | Decision | Needed by | Recommendation | **Decided (owner, 2026-11-17)** |
|---|---|---|---|---|
| P5-1 | Identity providers | 5A | LDAP(S) against AD first, behind an interface; OIDC next | **Both AD and Entra ID**; timing left to the assistant → AD/LDAP in 5A, **Entra ID in 5A.2** right after 5A is verified on the host (D68) |
| P5-2 | Local accounts | 5A | one bootstrap administrator from the CLI | **Accepted: exactly one local account, no others can be created.** Password set by the CLI, or at the first sign-in with a one-time setup code the CLI prints; recovery **by the CLI only** (D68) |
| P5-3 | How roles come from the directory | 5A | admin chooses groups; maps groups → app roles | **The administrator chooses how roles are read** (groups, a user attribute, or the OU a user sits in), then maps directory roles — or single users — to Gaia roles (D68) |
| P5-4 | Review granularity | 5B | column, with table purpose, grain and category | **Accepted**, and the item shows **everything Gaia knows** about the column, summarised, with a dive-in (D69) |
| P5-5 | Need-to-know model | 5C | categories, all required, deny by default | **Accepted, plus a per-user "PII processor" permission** (KVKK): without it personal data is shown masked, with it unmasked — now, in 5C (D69) |
| P5-6 | Who sets categories | 5C | Gaia proposes, SME confirms, admin grants | **The administrator evaluates and decides** every access decision (SMEs, Gaia roles, who sees PII); a new **auditor** role audits the administrator's work, mostly from reports (D70) |
| P5-7 | Who sees the SQL | 5D | not `user`; SMEs and admins in an audit view | **Accepted, and auditors too** (D70) |
| P5-8 | Sessions | 5A | server-side, idle 30 min, absolute 8 h, re-check 15 min | **Accepted** |
| P5-9 | Listening beyond `127.0.0.1` | before a pilot | only behind TLS, after 5A | **Accepted** |
| P5-10 | A directory to verify against | 5A | the company's test AD, or a Samba AD DC container | **Accepted** |
| P5-11 | Start Phase 5 before 3C finishes | now | yes | **Accepted** |
| P5-12 | The cut line | now | 5.0–5D and a read-only runs view in Phase 5; operations later | **Accepted** |

Smaller points settled by D70: an administrator may grant to a role they belong to
themselves — every grant is audited and is in the auditor's report; an SME assignment does not
grant chat access to that data source — review and chat are separate rights.

### Estimate

Rough, as always re-estimated at each stage gate: 5.0 S (1–2 days) · 5A L (5–7) · 5B L (5–8; the
carry-forward is the risk) · 5C L (5–7) · 5D M (3–4) · 5E read-only S (1). LLM use is small:
proposing categories can reuse the stored meanings; Gaia's role suggestions are optional.

### Risks

| Risk | Mitigation |
|---|---|
| Need-to-know leaks through metadata (context, options, narratives, errors) | enforced in retrieval **and** the guard; refusals name nothing; a blocking adversarial suite |
| Directories differ (AD vs OpenLDAP schemas, nesting, referrals, size) | `ldap3` with AD and RFC 2307 group models, paged searches; offline mock plus a real AD on the host |
| SME reviews lost to re-reasoning | carry-forward in the service, tested before any review screen ships |
| Administrators granting themselves access | every grant audited and visible; four-eyes later if wanted |
| Session theft, CSRF once the API leaves localhost | HttpOnly + SameSite cookies, a CSRF token, TLS before binding beyond `127.0.0.1` (P5-9) |
| Scope creep (operations, model switching) | the cut line (P5-12) |
| The sandbox has no directory | the mock directory in tests; host verification items per stage |

### Gate

- [x] Phase 4 gate ticked (owner, 2026-11-17)
- [x] **Approved to start Phase 5** with this staged plan (owner, 2026-11-17: "I approve Phase 5
      plan … Let's start"; every decision answered, D68–D70); P5-1 … P5-3, P5-8, P5-10, P5-11,
      P5-12 answered (the others before the stage that needs them)
- [ ] 5.0 complete (triage fixes; may run before the approval above) → 5A *(on the host, D68: T1
      closed, T5 and T6 seen working; the golden run's dip to 90 %: T6's prompt sentence withdrawn
      (D72); three fresh runs of the D72 prompt since — 97, 90, 93 % — so the model's spread is
      ±4 points and D72 stands (§26c F6))*
- [x] 5A verified against a real directory on the host *(D77 run, §26e: the Samba AD — LDAPS with
      its CA verified, users, roles, nesting, grants, every role in the browser; owner, 2026-11-17:
      "everything worked as expected in the web UI")*
- [x] 5A.2 (Entra ID) verified against a real tenant *(D77 run: all six checks with the secret's
      Value, Sign in with Microsoft in the browser. Found: a Microsoft 365 group counted as a role,
      fixed in D79; GAIA-Finance left out as not a security group — the probe now names it, §27d)* → 5B
- [x] 5B verified by the owner as SME on `demo_pg` (g07 included) → 5C *(D80 run: the owner's decision
      on `finance.invoices.status` — codes and the glossary — made g07 right in all four fresh runs,
      with no change to code or prompts; §28a)*
- [x] 5C adversarial suite passing → 5D offered to the `user` role *(724 passed on the host, D77;
      need-to-know in the browser as expected, owner 2026-11-17)*
- [ ] 5D and 5E (read-only) complete → **Phase 5 done** *(D80 run: the chat works in the owner's
      browser; "Show the SQL" for SMEs fixed in D81. Everything of Phase 5 is verified on the host —
      **the owner's tick closes the phase**; GAIA-Finance in the Entra tenant is a tenant setting)*

---


## Phase 6 — Evaluation & calibration · **approved 2026-11-17 (P6-0 … P6-7 as recommended) — 6.0, 6A, 6B, 6C, 6D part 1, 6E and 6G's measurement built (D78–D89); 6F to come; P6-8 (what the fresh net must catch) open**

**Goal.** Every quality claim Gaia makes is a number **with an interval**, measured the same
way every time; a regression is seen **before** a build ships; and a score means what it says —
"0.8" is right about 8 times in 10, for the model that produced it.

### What Phases 3–5 left for this phase (2026-11-17)

| Have | Where | Why it is not enough yet |
|---|---|---|
| Column scores (`heuristic-v2`), their evidence ledger, `calibrated_score` = identity | 3B, OUTLINE §12 | the weights are placeholders; nothing says how often a 0.8 is right |
| **Human labels**: every SME decision (5B: accept / correct / reject, signed, per version), the owner's 40 columns of `nge_small` and the 3C audit | 5B, 3C | not collected into one place, not mapped to "right / partly / wrong", not used |
| Golden runs (`demo_pg`, 32 questions), retrieval recall, clarification records (D61) | 4B, 4D | one run per build, files in `evaluations/`; a re-run into the same directory is a replay (the D75 run read two replays as fresh runs) |
| The 3C harness and an LLM judge | 3C | the judge agrees with the owner on 86 % of columns and is the more lenient one in 9 of 13 disagreements (§26c F7): good for triage, not for calibration |
| The answers' confidence, shown as a level with reasons (T7) | 4C, 5D | uncalibrated: g07 (wrong) was "medium", g01 (right) "low" |
| Category proposals with their basis (D77), and the placements people confirmed | 5C | nothing measures how often a proposal is kept |

And one measured fact that shapes the design: **the golden run varies by about ±4 points from run
to run** with an unchanged prompt (97 / 90 / 93 %, §26c F6). A single run cannot tell a
regression of less than ~7 points from noise, so Phase 6 compares *sets* of runs, question by
question, and says how sure it is.

### Design

**6.0 — Phase 5 closed first.** The triage of §26e; the Phase 5 gate; the 3C gate (P6-0); D78
if the owner wants it built (an account is the directory's object, not its login).

**6A — one evaluation store, one report.** Schema `evaluation` (migration number at creation, R4):
`evaluation_run` (kind — golden, retrieval, semantics, categories —, data source, build, git
commit, model, prompt versions, snapshot, config hash, replayed or fresh, started/finished, cost)
and `evaluation_item` (one question or column per run: expected, got, correct, the confidence
shown, latency, tokens, the query run). The golden, retrieval and 3C tools write there as well as
to their directories; a **fresh** run and a **replay** are recorded as what they are, and
`--out` refuses an existing directory unless `--replay` or `--resume` says which is meant (F6).
`make quality TARGET=… ` writes one report per data source and model: correct and executable
answers with a 95 % interval (Wilson), clarification asked-back rate and best-guess agreement
with its Beta lower bound (OUTLINE §36, method 1), retrieval recall, column-meaning accuracy
against **human** labels with its interval, calibration error before and after (6C), latency
p50/p95, tokens and cost — and, between two runs or two sets of runs, **the difference per
question**. An *Evaluation* page (administrators, auditors; read-only, like *Runs*) shows the
latest reports and their trend.

**6B — labels from people, in one place.** A label is a person's verdict on Gaia's meaning of a
column, at a version: an SME's *accept* → right; *correct* → wrong when the name, purpose or role
changed, partly when only unit, entity or other names did; *reject* → wrong; *not used* → no
label. The owner's 3C labels and audit count the same way; a stale decision (its column changed)
stops counting. The LLM judge stays for triage — its agreement with people (Cohen's κ) is in every
report — and never feeds a calibrator or a gate. Because the SME sees Gaia's certainty while
deciding, a small random share of review items (P6-5: 10 %) is shown *without* it, to measure
whether people anchor on it.

**6C — column scores that mean what they say.** Per reasoning model (R3, A7) and scoring version:
isotonic regression from `raw_score` to P(right), fitted on the labels of 6B — Platt scaling below
the minimum of P6-1, the identity (and "uncalibrated" on the screens) below half of it. Fitted
with cross-validation and judged on held-out labels: expected calibration error, Brier score and
a reliability diagram, **against the identity**. The calibrator's version is stored beside every
score (`calibrated_score`); the bands (`accept_at` …) are re-expressed as probabilities (P6-3),
so the review queue ranks by calibrated uncertainty. Pooling across data sources of one model is
partial (hierarchical), so a new data source starts from the others' calibration instead of from
nothing.

**6D — answers whose confidence means what it says.** P(the answer is right) from what Gaia knows
when it answers — the retrieval scores, repairs, how many meanings it used are SME-confirmed or
uncertain, whether it clarified, the question's kind — fitted (logistic, then isotonic) on the
golden runs and on **people's feedback**: under an answer, "Was this right? yes / partly / no" with
a note (P6-4). A "no" opens a review item for the data source's SME, who decides what was wrong (a
meaning, a missing definition, the model); only a decided item becomes a label. People who only
chat still see a level — its thresholds now fitted ("high" = right at least 95 % of the time, with
the interval) — and SMEs, administrators and auditors the probability (P6-2).

**6E — regressions seen before they ship.** Two nets. *Replayed* (no model, seconds, in `make
test`): every golden question's prompt is compared byte for byte with the last accepted one, and
the recorded answers are replayed through today's guard, executor and scorer — a code change that
alters a prompt or an outcome fails the suite and names the question. *Fresh* (the model, minutes):
a change that alters a prompt — or a new model — runs the golden set **three times**, and is
compared question by question with the baseline's three runs (a paired bootstrap of the
difference); it passes when the lower bound of the difference is above −3 points (P6-2). The
acceptance test of the phase is a deliberate regression: a prompt with its JOINS rule removed, and
a guard that lets a fan-out join through, must both be caught.

**6F — more and harder questions.** `nge_small`'s golden set (P4-1, P6-6: Gaia drafts ~30
questions from the schema and the meanings, the owner corrects them) and its first T-SQL golden
run; golden runs *as a person* for each role pattern (`--as`, need-to-know in force); the
clarification measurements of D61 with their intervals, which decide D61 (b) — answering without
asking when the agreement's lower bound clears 90 % for that kind of ambiguity.

**6G — category proposals measured.** How often Gaia's proposal is what people confirmed, per
basis (D77) and per data source; the weights of D77 fitted to that record (still a deterministic,
explainable rule — the fit chooses its numbers, not a model at run time).

*Cut line:* 6A–6C and 6E are the phase; 6D's feedback control, 6F's drafted questions and 6G can
move to Phase 7 if time is short. Scheduling runs, switching models from the screen and the
overnight refresh stay in Phase 7 (P5-12); 6A makes a model switch *measurable*, which Phase 7's
"impact preview" needs.

### Order of work

| Step | What | Verifiable in the sandbox | Needs the host |
|---|---|---|---|
| 6.0 ✅ | §26e triaged (§27); 3C gate (P6-0 ✅); D78 built; §27d triaged (§28); the Phase 5 gate is the owner's | D78 with the LDAPS directory | ✅ §27d |
| 6A ✅ | `evaluation` schema; golden runs write to it; fresh vs replay; `make quality`; older runs imported; the meanings of a run and an item's SQL (D81); the *Evaluation* page | fully, with the scripted model | ✅ a real report (§28a); the page, §28d |
| 6B ✅ | labels from SME decisions (`make labels`, in `make quality`); the blind share of review items (P6-5); the owner's 3C audit as labels and the judge's κ (D85) | fully | ✅ the owner's decisions, some blind (§29a); the 3C import (§29d step 1) |
| 6C ✅ (built) | calibrators per model and scoring version (P6-1, counted per column or question since D89); held-out ECE/Brier/reliability against the identity; `--apply` and `make rescore`; bands as probabilities (P6-3); "uncalibrated" on the screens (D86) | with synthetic labels (known distributions) | a fit on real labels — P6-1's count (13 SME + 17 N-P audits today) |
| 6D ✅ (part 1) | "Was this right?" → the SME's *Answers people doubted* → a decided label; answer calibration from golden items and decisions; P(right) for SMEs/admins/auditors once calibrated (D87) — *levels fitted on real data: part 2* | fully | people's feedback over weeks (§29d step 5 starts it) |
| 6E (replayed net ✅; the fresh net's acceptance open, P6-8) | the replayed net in `make test` (D82; narration rows as a set, D84), its deliberate regressions caught; the fresh net is 6A's comparison over three runs; `make golden --regress joins-rule` and the cost per run in a comparison (D87); regression runs kept out of reports and labels (D89) | fully (scripted model) | the net on the host's data (§30e step 0 — `29_tests.out` skipped it); the JOINS-rule regression ran: **not caught, and rightly** (§30b-7) |
| 6F | `nge_small` golden set (drafted, corrected), T-SQL golden run, persona runs, D61 (b) | drafting only | the owner's corrections; SQL Server |
| 6G ✅ (measured; weights not fitted) | proposal accuracy per basis — today's rule with each confirmation hidden in turn, and the proposals people saw (`make access ARGS=accuracy`, in `make quality` and on the page; D88) — *fitting D77's weights waits for more confirmations* | with confirmations made in the test | the owner's ~50 confirmed placements (§29d step 6) |

### 6.0 and 6A results (2026-11-17, sandbox)

**6.0** — the D77 run triaged (Phase 5, "Owner runs on D77"; OWNER_VERIFICATION §27); the 3C gate
closed (P6-0); **D78 built**: `auth.account.pending_external_id` / `identity_changed_at`
(migration `0017`); when the directory's stable id behind a login (AD `objectGUID`, `entryUUID`,
Entra's object id — never a DN, which moves) differs from the account's, the sign-in is refused
and audited once (`identity_changed`), an open session ends at its re-check, sync leaves the
account as it was and counts it; an administrator decides — **re-link** (the same person: the
account and its grants follow the new object) or **start anew** (someone else: its own grants
revoked, its sessions ended; grants to directory roles apply as the directory says) — on
*Accounts*, by `make auth ARGS="account relink|renew DIR/LOGIN"` or `… DIR --all` for a test
domain provisioned again. Verified with the LDAPS directory: finn's object changed → refused →
the banner → re-linked in the browser → signed in.

**6A, part 1** (D80) — `evaluation.run` and `evaluation.item` (migration `0018`). Every `make
golden` is recorded with its build, commit, model, question set (by content), the person it
answered as, and its **mode**: *fresh*, *replay*, *mixed* (resumed) or *imported*; its report
says at the top how many answers were asked now. A directory holding recorded answers is refused
unless `--resume` (a replay reproduces a run — the D75 misreading). `make quality`: the last N
measured runs pooled — correct and executable answers with 95 % Wilson intervals, the spread per
run, questions right always / sometimes (flaky, with counts) / never (with the reason), the
clarification measurements with intervals (D61's lower bound against 90 %), calls, tokens and
time — and a comparison of two sets of runs question by question (paired bootstrap, P6-2's −3
points: passes / fails / cannot tell), by run ids or `--compare-builds`. `--import` brings older
run directories in (`imported`, once per directory). Verified: the statistics against known
values, the selection (replays never counted, one question set at a time), a comparison that
finds the one question a worse model gets wrong; golden runs through the real tool with the
scripted model — fresh, refused, `--resume` recorded as a replay — and the report.

Not yet: the *Evaluation* page (6A part 2), retrieval and 3C runs in the store, and everything
from 6B on. Tests: **725 passed, 10 skipped** in the sandbox (+11 since D77), web **46** (+4); migrations `0017`, `0018` down and up.

### 6A part 2, 6B, 6E results (2026-11-17, sandbox; D81–D83)

**6A part 2.** The *Evaluation* page (administrators; auditors through *Audit*) over
`/api/evaluation/*`: the runs — fresh, replayed (marked, never counted), resumed, imported — with
their build, model and meanings, and the report (pooled shares with intervals, flaky questions,
the questions wrong in every run with their latest SQL, best-guess agreement, and the newest
build against the one before). **D81**: a run records a fingerprint of the meanings it answered
with — current semantic versions, SME decisions, glossary terms, confirmed joins — and a
comparison whose sides differ says so; an item keeps its final SQL (migration `0019`).

**6E, the replayed net (D82).** `tests/integration/test_golden_net_pg.py`, in every `make test`:
the 32 golden questions through retrieval, generation, the guard, the executor, repairs,
narration and scoring, with a scripted model that is wrong on purpose four times; each prompt's
fingerprint (approximate numbers left out: a server's sampled statistics must not move it) and
each outcome against `tests/fixtures/golden_net/demo_pg.json`. Verified: deterministic over fresh
worlds; a guard letting a fan-out join through moves g03's outcome (repairs 1 → 0), a prompt
without its JOINS rule moves g02's prompt — both caught, in the test itself. `make
golden-net-accept` accepts an intended change; a server whose data profiles differently keeps its
own net (`GAIA_GOLDEN_NET`).

**6B, labels from people (D83, part 1).** `gaia.evaluation.human_labels`: every SME decision on a
machine meaning, with that meaning's model, prompt version, scoring version and score, as right /
partly / wrong (accept; a correction of role or purpose, of name, entity or unit, or only of
other names, personal data or the values' meanings; reject; not used gives none); a second
decision on the same meaning replaces the first; stale decisions still count. `make labels`;
`make quality`'s "against people" section (counts, AUROC per model, agreement per score band,
accepted with and without certainty). **P6-5**: `semantics.review_blind_share` (0.10 in the
configuration) — those review items show no score, band, reasons about certainty or Gaia's stated
doubts, anywhere (queue, table page, column page, history); a filter on why items are queued
leaves them out (computed in SQL with the same hash); the decision records `blind`. Verified on
the scripted world: every mapping, the replacement, the blind views, the filter's equality with
the Python hash, and in the browser (11 of 166 items blind on `demo_pg`).

Tests: **734 passed, 10 skipped** in the sandbox (+9 since D80), web **49** (+3); migration `0019` down and up.

### Owner runs on D83 (2026-11-18) — triage, and build D87

OWNER_VERIFICATION §29. **743 passed, 1 failed**: the replayed net on the owner's server — `g08` and
`g09` have no `ORDER BY`, the server returned their groups in another order than the sandbox, and
the narration prompt carries the rows; the net was too strict (D84a). 6B works on real decisions:
13 labels (12 right, 1 wrong), 5 of them blind; the AUROC of one wrong label is no measurement
(D84c). Three fresh D83 runs: 28, 29, 29 of 30 — pooled 96 % (89–98 %); D83 against D80 **cannot
tell** (−0.3 points, −3.3 to +2.5), with the meanings warning (the D80 runs' meanings were "unknown"
because they predate D81, not because they were imported — D84d). **o06** wrong in all seven runs
since D80, now with its SQL: the formula of the clarification followed, its "not cancelled"
dropped (no join to `sales.orders`). Retrieval or reading? The runs did not keep their context —
every item now does (D84b), and §29d step 3 answers it. The SQL link (D81) and the *Evaluation*
page work in the owner's browser; GAIA-Finance exists in the tenant. Phase 5 is verified; the
owner's "let's continue with the next stage" is read as its acceptance, the tick left to him.

### 6B part 2, 6C, 6D part 1, 6E part 2 results (2026-11-18, sandbox; D84–D87)

**D84** (triage): `gaia.evaluation.net.fingerprint` sorts a narration's result rows; the stored net
re-accepted (15 narration fingerprints moved, no outcome). `evaluation.item.detail` (migration
`0020`) keeps the context's objects, the expected objects missing from it, the interpretation,
assumptions, clarification and confidence of the final answer; `make quality` adds *Why answers
were wrong* (retrieval against reading) and *The confidence Gaia showed, against the reference*
(right k of n per level, with Wilson intervals), and explains every question wrong in every run.

**6B part 2 (D85).** `make labels ARGS="--import-3c DIR"` imports the owner's filled 3C audit into
`evaluation.label` — verdict, the judged answer's score, model and condition, the judge's verdict —
once per row; `PRODUCTION_CONDITIONS` (N-P, N-P-raw) mark the labels calibration may use. The judge's
agreement with people (share, **Cohen's κ**, more lenient / stricter) is in `make labels`, `make
quality` and the page, and read by nothing else. Verified on a 3C directory with two conditions, an
unfilled row and a row whose answer is missing; a wrong `TARGET` refused.

**6C (D86).** `gaia.evaluation.calibration`: pool-adjacent-violators isotonic regression, Platt
scaling (Newton, a small ridge), the identity; P6-1's minimums choose (`calibration.*`, a section of
its own so the stored scores' inputs do not move); five folds by subject (a column, a golden
question — never on both sides); ECE (ten bands), Brier, the reliability rows, on held-out labels
against the identity; a Wilson lower bound from the labels behind each probability (the isotonic
step; Platt: the score's tenth). `gaia.evaluation.calibrators` keeps fitted calibrators
(`evaluation.calibrator`; one in use per kind, model and scoring version — a partial unique index),
never the identity. `make calibrate` fits and judges every model; `--apply` only below P6-1 or a
calibrator that does not beat the identity on both measures is refused (`--force`, to be logged);
`--reset`; `--list`. In use, `score_column_v2` writes P(right), its lower bound and the
calibrator's version, and bands by P6-3 (accepted only with P ≥ 0.90 and bound ≥ 0.80); the
calibrator's version is part of the stored inputs only while one is in use, so `make rescore`
re-scores exactly then and a store without one keeps its hashes. Verified against known
distributions (P(right) = score² → isotonic halves the ECE and beats Brier; an already calibrated
score is not "improved"; 13 labels stay the identity; a probability from five labels cannot accept),
and in the store: a calibrator put in use re-scales `demo_pg`'s meanings on rescore (accepted,
queue shorter), and `--reset` restores them. Full pooling over data sources per model; partial
(hierarchical) pooling waits for a second data source with labels.

**6D part 1 (D87).** `chat.feedback` (migration `0020`): "Was this right?" — right, partly, wrong,
with a note — on an answer of one's own conversation (`PUT /api/messages/{id}/feedback`); right is
counted; partly and wrong open an item for the data source's SMEs (`GET /api/review/{ds}/feedback`,
`POST …/{id}/decision` with the decision and, if not right, the cause; audited
`feedback_decided`); withdrawn before a decision it closes, changed after one the decision stands.
The chat shows the control and the decision under the answer; *Review → Answers people doubted*
shows the item with its table, interpretation, assumptions, confidence and SQL. Decided items and
golden items (with the score they showed, D84) are the answer labels; `ask` turns the heuristic
score into P(right) and the level by `calibration.answer_high`/`answer_medium` once an answer
calibrator is in use; SMEs, administrators and auditors see the percentage and its bound, others
the level (P6-2); the heuristic says "uncalibrated". Verified over HTTP (owner only, answers only,
CSRF, the SME's 403 elsewhere, the audit event, the label) and in the browser (finn → akaya →
finn).

**6G (D88), measured.** `CategoryService.accuracy`: for every object a person confirmed, the proposal
today's rule makes with that one confirmation hidden (its neighbours' stay, as for a new object) —
agreement per basis, the misses with their reasons, the objects left without a proposal — and the
proposal people actually saw before their first decision on each object, and whether they kept it
(a later change of mind is not about a proposal). `make access TARGET=… ARGS="accuracy [--why]"`,
a section of `make quality` and of the page. Both lean towards agreement — people decided with the
proposal on screen — and say so. Fitting D77's weights to this record waits until it holds more than
the ~50 placements the owner confirmed: eight weights on fifty labels would fit noise.

**6E part 2 (tooling).** `make golden --regress joins-rule` (`PromptRegression`: the SQL prompt
without the line that starts "- Join only as listed under JOINS.", a run that fails loudly if the
line is gone), recorded as `<build> + deliberate regression: joins-rule`; a comparison now also
prints repairs, model calls and tokens per run. The acceptance test itself is the owner's three
runs (§29d step 4).

Tests: **755 passed, 10 skipped** in the sandbox (+21 since D83), web **61** (+12); migration `0020`
down and up; DDL identical to `create_all`. Not yet: answer levels fitted on real data (6D part 2),
6F, 6G; a calibrator in use needs P6-1's labels.

### The D88 host run, and D89 (2026-11-18)

The owner ran §29d up to step 4 on D88 (OWNER_VERIFICATION §30). **Confirmed:** the 3C audit
imported (94 labels, idempotent); three fresh D88 runs 28, 26, 27 of 30 — pooled 90 % (82–95 %),
executable 100 %; the D84 sections on real runs: **o06 is retrieval** (the context lacked
`sales.orders` in all six D88 runs; the model then invented a filter on `order_lines.condition`),
wrong answers split into retrieval (3) and reading (6), the levels against the reference;
the regression tooling. **Not verified:** `make test` ran without the §9 exports (82 skipped — the
instructions had dropped the check). **Found:**

* the deliberate regression was read as answer labels, and 180 labels of 30 questions were counted
  against P6-1 as 180 — the report offered a Platt calibrator "beating the identity";
* regression runs would have been the newest "build" for `--last N` and `--compare-builds`;
* D88 against D83 (no prompt changed): −5.6 points with an interval of −11.1 to −1.1 — 4 of 30
  questions moved, all down; exact sign test p = 0.125, whole-run permutation p = 0.20. The
  per-question bootstrap is over-confident with few discordant questions and blind to what a
  session shares (the provider's model, "always the latest");
* the JOINS rule's removal: +1.1 points, repairs 1.0 → 1.0 — **not caught, and rightly**: the guard
  refuses what the rule forbids and none of the 30 questions needs two child tables of one parent;
* the guard accepts a **chasm trap** (order lines → orders ← payments: two verified FK joins; in
  `demo_pg` 15,347 orders have 2+ payments), which the prompt's JOINS rule even advises ("or go
  through the parent table").

**D89 (no migration).** `make test-host` (the §9 exports required, every skip fails the run) and a
skip report in every run; `make labels` and `make quality` refuse a name that is no data source;
`REGRESSION_MARK` / `is_regression` (`gaia.evaluation.net`): regression runs are never answer
labels, are left out of `--last N` and `--compare-builds` (counted at the top of the report) unless
they are the newest runs — then compared with the build they regressed, with a line saying only
*fails* is a catch; P6-1's minimums count **subjects** (columns, golden questions) once each, as
not right if any of their labels is — as P6-1 was written ("150 labelled columns"); the confidence
section counts pre-D84 answers as *not known*; 6G's "0 of 0" says what it is.

Tests: **759 passed, 10 skipped** in the sandbox (+4); web unchanged (61).

### Deliverables

- Back end: `db/models/evaluation.py` and its migration; `gaia/evaluation/store.py` (runs and items),
  `labels.py` (6B), `calibration.py` (6C, per model and scoring version), `answer_confidence.py`
  (6D), `compare.py` (paired comparison, bootstrap), `quality.py` (the report); `api/evaluation.py`
  (read-only); the feedback endpoint on a chat message
- CLI: `make quality`, `make calibrate` (fit, report, `--apply`), `make golden --runs 3`,
  `make compare ARGS="--baseline RUNS --candidate RUNS"`
- Web: the *Evaluation* page (administrators, auditors); "Was this right?" under an answer; the
  probability for SMEs, administrators and auditors
- Tests: calibration against known distributions; the replayed golden net; the deliberate
  regressions; label mapping; feedback → review item; every number of the report recomputed from
  its items
- Docs: OUTLINE (evaluation and calibration as built), USER_GUIDE (reading the report), a
  measurement note per host report in `docs/measurements/`
- *As built (D80–D87):* `evaluation/store.py`, `quality.py` (comparisons live here, not in a
  `compare.py`), `human_labels.py` (6B), `calibration.py` and `calibrators.py` (6C, and 6D's answer
  calibration — no separate `answer_confidence.py`), `feedback.py` (6D), `net.py` (6E),
  `sections.py` (the report's 6B–6D parts); `make labels`, `make calibrate`, `make golden
  --regress`; the feedback endpoints in `api/chat.py` and `api/review.py`; OUTLINE §44

### Acceptance criteria

- [ ] One command (`make quality`) produces the report for a data source and model: correct and
      executable answers with intervals, clarification metrics with intervals, retrieval recall,
      column-meaning accuracy against human labels, calibration error, latency, tokens, cost
      *(D87: all but retrieval recall, which stays in `make retrieval-eval`; and why answers were
      wrong, per question)*
- [ ] Reproducible: two reports of the same runs are identical; a replayed run is marked as one
- [ ] Calibration improves over the identity on held-out human labels — ECE and Brier, measured,
      per model — or the report says there are too few labels (P6-1) *(D86: built and verified on
      synthetic labels; on the host today "too few labels")*
- [ ] A deliberate prompt regression (JOINS rule removed) and a deliberate code regression (a
      fan-out join allowed) are both caught — the first by the fresh net, the second by the
      replayed one, which runs in `make test` *(D88 on the host: the replayed net catches both;
      the fresh net did not see the JOINS rule's removal — +1.1 points, repairs unchanged —
      because the guard already refuses what the rule forbids and no golden question needs it.
      What the fresh net must catch is open: **P6-8**)*
- [ ] The golden run is reported over three runs, and a change is compared question by question
      with its baseline (P6-2)
- [ ] "High" confidence answers are right at least as often as their threshold says, with the
      interval shown (6D; P6-2) *(D84/D87: the report shows it per level from the next host run;
      the levels follow P(right) once an answer calibrator is in use)*
- [ ] The judge's agreement with people is in every report and no calibrator or gate reads it
      *(D85: built; the owner's 94 audits bring it, §29d step 1)*
- [ ] The category proposals' accuracy per basis is reported (6G, if not cut)

### Decisions needed

*All eight decided by the owner on 2026-11-17: "P6-0..P6-7. Agreed with your recommendation."*

| # | Decision | Needed by | Recommendation (= the decision) |
|---|---|---|---|
| P6-0 | The 3C gate: criteria 1–3 met, criterion 4 (owner audit ≥ 90 %) at 86.2 % | now | **Accept 3C** with criterion 4 recorded as not met; the judge becomes triage only (6B) |
| P6-1 | How many labels before a calibrator is used (per model) | 6C | **150 labelled columns with at least 30 wrong or partly wrong**; Platt from 75; identity below |
| P6-2 | The primary gate, and what confidence users see | 6D, 6E | **Correctness on the golden set over 3 runs**, a change passing when the lower bound of the paired difference is above −3 points; users see levels with fitted thresholds, SMEs/admins/auditors the probability |
| P6-3 | Bands as probabilities | 6C | `accepted` when P(right) ≥ 0.90 with the calibrator's lower bound ≥ 0.80; the rest as now |
| P6-4 | "Was this right?" under answers | 6D | **Yes**, for everyone who chats; a "no" becomes an SME review item, a label only once decided |
| P6-5 | A blind share of review items (no certainty shown) to measure anchoring | 6B | **10 %**, randomly, reported |
| P6-6 | `nge_small`'s golden questions (P4-1) | 6F | Gaia drafts ~30 from the schema and the meanings; the owner corrects them (an hour) |
| P6-7 | A second model through the same harness (D15: Claude or Gemini "may be evaluated later") | 6A | Optional, after 6E: the report compares models like builds |

*Open since the D88 host run (2026-11-18):*

| # | Decision | Needed by | Options — and the recommendation |
|---|---|---|---|
| P6-8 | **What the fresh net must catch, and how it says so** (replaces the JOINS-rule half of the acceptance) | 6E's acceptance | **The yardstick** — (a) keep the JOINS rule and add questions that need it: even then the guard refuses what the rule forbids, so its removal can only cost repairs; (b) **a regression whose damage is known in advance**: `--regress drop:<object>` takes one object out of every context, and `golden.yaml`'s `objects` say which questions lose what they need — `finance.invoices` 1 (g07), `store.stores` 5, `product.products` 8, `sales.orders` 14; (c) a weaker model (P6-7) — realistic, but its damage is not known in advance. **The verdict** — the per-question bootstrap resolves 3.3 points a question: with 3 runs a side it says *fails* reliably only when ~6 questions break, and an unchanged build *passes* only 40–50 % of the time (simulated on the host's flakiness); more runs barely help, more questions do. **Recommended:** (b) as the yardstick, at two sizes — `store.stores` (5 questions) must be *caught*, `finance.invoices` (1) documents the limit; a two-part verdict — **any question right in every baseline run and wrong in every candidate run is named and blocks** (a deterministic break of even one question is caught; on the host's flakiness a false alarm ~3 % a comparison; none on D83→D88 or D88→regression), and the overall difference with an interval that resamples runs as well as questions; the baseline re-run **in the same session** as the candidate (provider drift, §30b-6); 6F's questions to resolve smaller losses. The chasm trap is fixed in the guard either way (a correctness fix, caught by the replayed net, with golden questions that need sibling aggregates) |
| P6-9 | Pool `deepseek-v4.1-flash` with `deepseek-flash` for calibration (§21f: renamed) | 6C, once labels near P6-1 | Pool them through a configured alias **only** for labels from before the rename; `deepseek-flash` is "always the latest", so its own labels stay per model id (the risk row above) |

### Estimate

6.0 S (½–1 day + the owner's run) · 6A M (3–4) · 6B S (1–2) · 6C M (2–3) · 6D M (3–4) · 6E M (2–3)
· 6F S (1–2 + the owner's hour) · 6G S (1). Model calls: the fresh net is 3 × ~72 calls (~6 min at
today's speed) per prompt change; calibration and the replayed net make none.

### Risks

| Risk | Mitigation |
|---|---|
| Too few labels on a small database: a calibrator that overfits | P6-1's minimums; isotonic only above them; pooling across data sources of one model; the identity and a visible "uncalibrated" otherwise |
| SMEs anchor on Gaia's certainty, so labels agree with Gaia too often | the blind share (P6-5) measures it; if it matters, blind review becomes the default for calibration items |
| Model variance hides or fakes a regression | three runs, paired per question, an interval — never one run |
| Feedback is sparse or biased (people report what surprised them) | feedback opens review items but becomes a label only when an SME decides; the golden set stays the gate |
| A provider changes the model behind a name (`deepseek-flash` "always the latest", §21f) | every run records the answering model's id (T3); a calibrator is per model id and flagged when the id changes |
| Scope creep into operations | the cut line: scheduling and model switching stay in Phase 7 |
| An answer calibrator fitted before a meaning calibrator is applied reads different inputs afterwards (the answer's score averages the meanings' scores) | after `--apply` of a meaning calibrator, refit the answer calibrator on golden runs made since (`make calibrate --kind answer`); the report's per-level check shows the drift |

### Gate

- [ ] Phase 5 gate ticked (after §27d: the chat in the owner's browser, Entra's Finance role, g07
      — all three done by §29a; the owner's "let's continue with the next stage", 2026-11-18, read
      as acceptance; the tick is his)
- [x] P6-0 … P6-7 answered *(owner, 2026-11-17: as recommended)*
- [x] **Approved to start Phase 6** *(owner, 2026-11-17, with the decisions; 6.0 and 6A built
      the same day, D78–D80)*
- [x] 6A → 6B (a real quality report on the host: §28a — 86 of 90 over four D80 runs; §29a — the
      D83 runs with their meanings, and 13 labels from the owner's decisions)
- [ ] 6C, 6D part 1, 6E part 2 verified on the host (§29d: the 3C import with κ, o06's cause,
      the deliberate regression, "Was this right?" end to end)

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
- *From Phase 5 (P5-12, proposed 2026-11-17):* starting and stopping runs from the admin
  screen, the overnight schedule (a worker, per-data-source locks, cancellation, run windows),
  and the LLM / embedding connections in the admin screen with an impact preview (a model change
  re-reasons everything; an embedding change re-embeds and may change `vector(1024)`)
- *From Phase 5 (P5-5):* row-level need-to-know (a manager sees only their store),
  aggregate-only grants, masking per role; OIDC/SAML if 5A ships LDAP only
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
| D28 | new | LLM-dependent runs use the configured gateway and are executed by the owner; the assistant's own model is not a Gaia provider (A10). *Refined 2026-11-14:* the assistant may act as the model through the replay provider for **functional** tests only; such runs are recorded as model `replay` and never reported as accuracy (D30) |
| D29 | new | 3B before 3A (owner request): reasoning consumes evidence through an explicit contract (`profiling/summary.py`) that 3A's profiler fills; until then evidence is catalog-only or supplied as JSON, and `profiling.enabled: false` makes catalog coverage final |
| D30 | new | Replay provider (`gaia.llm.replay`): requests to files, answers from files, model recorded as `replay`; because the skip key contains the model, the first real-model run re-reasons replayed tables |
| D31 | new | The evidence hash covers stable, meaning-relevant facts only; volatile detail (examples, ranges and top values of high-cardinality columns) is shown to the model but not hashed |
| D32 | new (3A) | Module definitions and view lineage live in the profile store (`profiling.module_definition`, replaced when the definition's hash changes), **not** in the snapshot; `STRUCTURE_HASH_VERSION` stays 2 (the planned bump to 3 is withdrawn). A changed view definition reaches reasoning through the evidence hash of that view and of the base tables that receive its aliases — exactly those re-reason; no global re-baseline |
| D33 | new (3A) | One profiling run per data source at a time: a session-level PostgreSQL advisory lock on a dedicated metadata connection (stricter than the planned per-table lock; it also enforces "one profiling session per target") |
| D34 | new (3A) | Inclusion tests are row-weighted (share of sampled rows found) and carry *specificity*; an integer pair is specific only if chance (key density ^ values tested) < 1 %. Non-specific pairs are not Stage 1 candidates; a non-specific claim test is weak support and never a contradiction |
| D35 | new (3A) | Stage 3 tests are derived per column from the stored claim and the model's join hints (`pending_claim_tests`); a column with a test still to run is escalated, not queued. A Stage 1 candidate test of a claimed join is adopted without a new query |
| D36 | new (3A) | Profiling is on in the shipped config (`profiling.enabled: true`; a config without the key stays off) and can be switched off or windowed per target (`targets.<name>.profiling_enabled`, `profiling_window`). The CLI refuses when disabled or outside the window; a window that closes mid-run skips the remaining tables (`window`) |
| D37 | new (3A) | Stage 1 join candidates are evidence (`profiling.verification_test`), not join paths: `semantics.join_hint` rows still come only from FKs, the model and SMEs |
| D38 | new (2026-11-15) | Model join hints have a lifecycle: `inferred` while the latest answer for the table proposes them, `stale` when it no longer does, `refuted` when a Stage 3 test of the claim finds < 50 % of rows in the target (a later pass restores `inferred`). Only `inferred` hints are tested and (Phase 4) used. Migration `0006` |
| D39 | amends D34 | Integer inclusion specificity is decided by the outcome: a pass or keepable share is specific only if chance alone (binomial, key density) would reach that many hits < 1 % of the time. The `density ** d` rule remains the pre-test candidate filter |
| D40 | new (2026-11-15), **final** (decided by the assistant at the owner's request) | A declared FK and a passed inclusion test state one fact — the values are keys of another table — and are read the same way whatever kind of table holds the column (fact, transaction, simplified transaction): a role says how the column is used; the dimension an FK points to is the *entity* of a `reference`, not a role. They **support** `reference`, `identifier`, `code`; are **neutral** to `dimension` (a denormalised lookup name), `flag` and `timestamp` (an integer date key with an FK to a date dimension); **contradict** `measure` and `free-text`. A single-column key fits a `code`. FKs are never required: where none are declared (the common case, A1) inclusion tests carry the same rules, and strengths stay as calibrated placeholders (trusted 0.60, untrusted 0.30). Telling fact tables from transaction tables is a table-level question, left to the table's meaning (`grain`) and Phase 4 retrieval. heuristic-v2 only |
| D41 | new (2026-11-15) | A claim test that cannot run (table budget, timeout, driver error, vanished target) is recorded as inconclusive (`method = not_run`): not evidence, not shown to the model, tagged `test_not_run` in the queue; `profile-tests --force` retries. Only an exhausted *run* budget leaves a test pending |
| D42 | new (2026-11-15) | Generation-2 prompts list alternate (unique) keys in the object list; generation 1 keeps Phase 2's list |
| D43 | new (2026-11-15, owner decision 1) | Re-reason only when the model would see something new; re-score (no LLM) when only the scorer's inputs changed. The scorer's inputs are hashed per version (`score_breakdown.score_inputs`: `SCORER_REVISION`, scoring config, bands, the rounded evidence plus test origins, importance inputs, inherited bases). `make rescore` = re-score only, never the model |
| D44 | new (2026-11-15, owner decision 3) | Pass-through view columns inherit their (ultimate) base column's current meaning and score; never reasoned, never queued; re-inherited by re-scoring when the base changes. Computed view columns are reasoned with the others as context |
| D45 | new (2026-11-15, owner decision 2) | Blank text ('' or spaces) is NULL in every profile metric, detector and exact count; its share is reported (`nulls_blank_text`) |
| D46 | new (2026-11-15) | Profiling order and statements: the sample first; columnstore one aggregate statement per column and exact distinct counts; large rowstore low-cardinality top-k in one `GROUPING SETS` scan (per-column fallback); degradations printed per metric and reason |
| D47 | new (2026-11-15, owner decision 5) | Stage 1 pairs tables profiled in earlier runs with parents profiled now (values re-drawn, one sample per table, tested pairs not repeated) |
| D48 | new (2026-11-15, owner decision 6) | Phase 4 may use a model join hint (`inferred`) whose test of the claim passed and is specific, ranked below declared FKs; the UI shows declared FKs and discovered joins separately |
| D49 | new (2026-11-15) | A failed inclusion test of a claimed parent contradicts a `reference` only when the column's values were found in no key at all; otherwise it refutes that parent's hint (D38) |
| D50 | amends the 3A time rule (owner, 2026-11-15) | `profiling.budget.time_rule: adaptive` (default): a statement starts when the elapsed time plus twice the slowest statement so far on the table (at least 5 s, at most the timeout) fits `max_runtime_per_table_s`; a cancelled statement counts at its full timeout. A pathological statement can overrun the ceiling by at most one statement timeout. `strict` keeps the hard rule |
| D51 | new (2026-11-15) | 3C harness: every condition runs today's pipeline (N-C re-run with v2 prompts); nothing is written to the metadata store; profiled conditions test their own claims in memory from Stage 1 evidence; reference = owner label / ground truth where present, else original names; named conditions are scored on labelled columns only; the owner audits 20 % blind to the judge's verdict |
| D52 | new (owner, 2026-11-16) | **PostgreSQL is the main dialect.** Every dialect-related change is built and tested on PostgreSQL first (the assistant reproduces that environment); the SQL Server counterpart follows, with tests, but SQL Server results no longer block a delivery. What only the owner can run is written to OWNER_VERIFICATION.md and answered asynchronously. *Refinements proposed by the assistant, **accepted by the owner on 2026-11-16**:* (a) "non-blocking" applies to **live** SQL Server runs; offline T-SQL tests (fake driver, `sqlglot` parsing, golden hashes, D26) stay in the suite and must pass; (b) **safety is never non-blocking**: a SQL Server result that shows a write path or a guard bypass stops SQL Server use until fixed, and the T-SQL guard's adversarial tests block Phase 4; (c) each asynchronous item has an id, the build it was written for (`make stamp`) and what depends on it, and an answer that arrives late is triaged before new work continues; (d) the PostgreSQL *data* is not realistic — `demo_pg` was designed by the assistant, so it proves machinery, not accuracy; accuracy stays on `nge_small` (3C) unless a realistic PostgreSQL copy exists (OWNER_VERIFICATION §14e) |
| D53 | new (2026-11-16) | A truncated answer (`finish_reason = length`) is asked again with at most half the columns of the chunk that truncated, up to 3 times (14 → 7 → 4 → 2); a truncated critique is asked again in halves. Answers that fit are asked exactly as before. The plan does not reserve room for thinking (not predictable: 340–12,437 tokens per call in 3C). `llm.max_output_tokens` stays 16,000: at ~15 tokens/s a larger budget outlasts `llm.timeout_s`, and it changes every request key. The evaluation recorder keeps truncated answers |
| D54 | amends D39/D3b (2026-11-16) | Integer inclusion specificity also uses a **neighbour control**: the same inclusion statement on a nearby integer of each tested value (value + a seeded offset of 1–97; not +1, which makes date keys look dense); the result is specific only if its hits are < 1 % likely at the larger of the neighbour rate and the density where the values lie. Robust to skewed values (counts with outliers), needs no new statement. A density or control query that fails is written into the test's detail |
| D55 | amends the 3C criteria (2026-11-16) | Criteria 1 and 2 compare two conditions on the columns both answered and both had judged; the report states how many were left out. A condition with a failed table or critique is reported as incomplete (exit 2) |
| D56 | new (owner, 2026-11-16) | **SQL Server is verified asynchronously, on delivered builds.** Every delivery ends in a commit and a build marker (`make stamp`: build, sqlglot version, git commit). The owner runs the SQL Server items on a delivered build when time allows and sends the outputs; the assistant reads each result against the build it names. A SQL Server finding is fixed in a later build and the item is run again there; nothing delivered is rewritten. OWNER_VERIFICATION keeps a **SQL Server track**: every item with the build it was written for, its status, and what it would change. Results never block a PostgreSQL delivery (D52), except safety (D52 b) |
| D57 | new (2026-11-16, Phase 4A) | **The SQL guard.** (1) What executes is rendered from the validated AST, never the submitted text; the rendering is parsed and checked again. (2) Allowlists, not denylists: root statement types (`SELECT`, set operations) and functions per dialect, checked by the name the target will see. (3) Objects only from the catalog snapshot, each resolved to one schema and qualified (PostgreSQL sessions use `search_path = pg_catalog`); columns through `sqlglot`'s qualifier. (4) Join paths are key classes of declared FKs, D48 hints, SME hints and view pass-through lineage: parent–child *and* siblings of one key are joinable; a join to a generated series or `VALUES` list is not a relationship between tables; comma, `CROSS`, `NATURAL`, `LATERAL`/`APPLY` and computed-value joins are unverified and rejected (`query.unverified_joins: reject`). (5) Row cap = min(`query.row_limit`, target `max_rows_returned`), added or lowered in the statement and enforced again at fetch. (6) Every run — accepted or rejected — is a `chat.query_run` pinned to its snapshot and the semantic versions of what it read (migration `0007`) |
| D58 | amends D57 (2026-11-16) | **No fan traps.** A join needs a join path *and one unique side*: a catalog key (PK, single-column unique constraint, unfiltered unique index), a derived table grouped by the join column or a single-column `DISTINCT`, or a generated series; otherwise `fan_out` is refused with what to do instead. View lineage is an alias (the view column is its base column), not a join edge. Retrieval offers only joins the guard accepts |
| D59 | new (2026-11-16, Phase 4B) | **Retrieval.** Lexical BM25 over names (abbreviations expanded), meanings, synonyms and readable low-cardinality values, plus pgvector cosine over the current meanings' `bge-m3` embeddings (exact scan, same model only), fused by reciprocal rank, rolled up from columns to objects, weighted by confidence 0.6 + 0.4 × mean score (R2: ranked, never filtered; abstained, SME-rejected, stale and unused meanings not used; SME-reviewed = certain). Objects: up to `retrieval.max_objects` above `relative_cutoff` of the best, connected by shortest join paths (FK and SME first, then through a view, then D48 hints), bridges added. Columns: whole objects up to `all_columns_up_to`, else keys, join columns, matches, then time and measures. Values only as the exposure policy allows (D24); uncertain and unreasoned meanings disclosed in the context. Lexical-only when embeddings are unavailable, said so. Deterministic |
| D60 | new (2026-11-16, Phase 4C; P4-2, P4-4, P4-8 owner) | **Answering.** One model call plans and writes the query (decision `answer` / `clarify` / `cannot_answer`, interpretation, assumptions, SQL, columns relied on); thinking off. Clarify always when two readings give different numbers, a relative period falls outside the data, or a needed join is not listed; without the model when retrieval finds nothing. Relative periods are written as date literals from "today" (`query.as_of`, else the real date), never the database's clock — reproducible, and the answer can name the period. Guard refusals, database errors and timeouts are repaired in the same conversation, at most `query.max_repair_attempts` (2). The user gets a table and a narrative (P4-2; the SQL only on request); the narrative model sees at most `query.rows_to_model` rows and never the values of personal-data columns when it is remote (D24); a failed narrative is replaced by a plain one. Every answer discloses its interpretation, assumptions and the unconfirmed meanings it relies on, and an explained confidence (mean certainty of those meanings, less repairs, hint joins, a capped result, assumptions). One `chat.query_run` per attempt, chained; every call audited. The default target stays `demo_mssql` (P4-8) |
| D61 | amends P4-4 (owner proposal, 2026-11-16; safeguards by the assistant — (a) accepted by the owner on 2026-11-16, (b), (c) pending) | **Clarify always — and remember.** Every clarification carries Gaia's best guess: the option it would choose, the kind of ambiguity (measure, period, population, entity, ranking, scope, other), and that option's SQL, interpretation and assumptions — checked by the guard, **never run for the user**. It is stored with the user's answer (`chat.clarification`, migration `0008`): the option chosen (or the user's own words), whether it was Gaia's, the run that answered. The user is not shown which option Gaia would choose (it would pull answers towards it and inflate the measurement). Choosing Gaia's option answers from the stored guess without a new generation call. **Learning is deferred and gated:** Gaia answers a kind of question without asking only when (a) on users' clarifications — never golden or test runs — its choice was the user's for that kind of ambiguity on that data source often enough (≥ 90 %, accepted; the assistant recommends reading it as the 95 % lower bound of the agreement rate, not the observed share — 27 of 30 is an observed 90 % but a lower bound of 0.77; 30 of 30 or ~100–200 at 95 % clear 0.9; OUTLINE §36), and the golden best-guess score agrees; (b) the answer then states the choice it made and offers the other readings (a soft clarification, R2); (c) what is learned is a definition ("a valuable customer = total order amount, cancelled excluded"), proposed to the glossary and confirmed by an SME before it applies to everyone; a user's own repeated choices may apply to that user only once identities exist (P5-1). No model is retrained: what changes is Gaia's memory and the prompts it writes |
| D62 | new (2026-11-16, Phase 4D) | **Golden runs.** Execution accuracy by results, never SQL text: columns matched by value (extras allowed; one of `identify_by` suffices), numbers equal at the precision shown, same row count, rows as a multiset or — `order_matters` — a sequence with ties on `rank_by` interchangeable, rows tied at a top-N cut interchangeable (the reference fetched 50 rows beyond its limit). Per question: `answer` — the first answer scored; a clarification instead is over-clarification, re-asked "as stated" and scored; `clarify` — asking back (or cannot answer) is right, a number a policy miss; with `clarified` and a reference, the best guess is run in the runner only and scored, then the question is re-asked with the user's answer and scored. Headline (Phase 4 acceptance) over every question with a reference, counting the final answer: executable, correct. References run through the same guard and executor. Runs are resumable (every model answer kept); golden runs and clarifications are recorded with `origin = golden`, outside D61's agreement |
| D63 | amends D59 (2026-11-16) | **Retrieval, from the owner's wording.** Question-side business synonyms (shop → store, client → customer, revenue → amount, vendor → supplier, representative → employee …; never time words); number and comparison words are stop words; "-us", "-is" words are not plurals. **Implied objects** (`retrieval.max_expansions`, 2): a ranking or aggregate brings the largest table referencing one of the two best objects; a period brings the dated table a fact without dates references. Both added as `expansion`, connected like any object |
| D64 | new (2026-11-16, Phase 4E) | **The chat API.** Conversations of turns (`chat.conversation`, `chat.message`, migration `0009`). A user message is a question or — when Gaia's last turn is an open clarification and the client does not say otherwise — the answer to it (an option's number or the user's words; D61). Follow-ups are read with the last `chat.history_turns` answered turns (question and what was computed; prompt v3). Server-sent events: `message`, `status` per stage, `result` (the table, before the narrative), one of `answer` / `clarification` / `cannot_answer` / `failed`, `done`; keep-alive comments while the model works. The live answer has every row; the stored turn keeps `chat.keep_rows` rows with personal-data columns masked, no SQL. An internal error is a `failed` turn. No authentication until P5-1: bind to 127.0.0.1 |
| D65 | new (2026-11-16, Phase 4F; P4-5, P4-6 on the recommendations) | **Data sources added at runtime.** `config/gaia.yaml` stays the bootstrap path; any other name is resolved from `catalog.data_sources`. Passwords encrypted in the application (AES-256-GCM, the key `GAIA_MASTER_KEY` in `.env`, bound to the row's id; `GAIA_MASTER_KEY_PREVIOUS` during a rotation), never returned, logged or audited. **Unusable until the read-only probe passes** (the 3.P probe, nine checks) and again after any change or after `datasources.reprobe_days` (30). Names unique across config and store; removal tombstones and wipes the secret; every action in `audit.connection_change` (fields, not values). CLI `make datasource`; the admin HTTP API is off (`datasources.admin_api`) until there is authentication (P5-1) |
| D66 | new (2026-11-16, the owner's question on dependencies) | **The owner's runs, in parallel.** Three real dependencies only: set up once per delivery; freeze 3C's inputs before SQL Server is profiled again (`inputs.json` in the run directory, written by the first run or `make evaluate-freeze`; later runs never read the store); judge a condition after it ran. Everything else in tracks by what they wait on — `quick` (databases), `gateway` (thinking off), `gateway-long` (thinking on), by hand — run by `scripts/owner_runs.sh`, each step to `verification/20_*.out` beginning with `make stamp`, a failed step not stopping the next. Two gateway jobs at once share its 4 slots (`GAIA_LLM_CONCURRENCY=2` each) |
| D67 | new (2026-11-17, Phase 5 step 5.0 — the triage of the D65 host runs) | **Triage fixes.** (T2) One writing reasoning or re-scoring run per data source: a PostgreSQL advisory lock taken *before* planning (`gaia.db.locks`, shared with profiling's D33 lock, whose key is unchanged); a second run is refused, a dry run takes no lock. (T3) Every evaluation call records the model that answered (`answered_by`); the 3C report lists the models per condition and flags a condition answered by two ids (a gateway's `provider/` prefix is one id). (T4) A refused call (4xx) carries the gateway's reason in its message (one line, ≤ 200 characters), and evaluation results keep the gateway's answer of a refused call — never a model answer. (T5) Clarification options are shown in an order seeded by the question and options (`chat.clarification.option_order`, migration `0011`, keeps the model's index of each); `default_option` is the shown position; a chat client never receives Gaia's choice (`clarification.default`, the best guess's interpretation and assumptions); `make clarifications` reports how often the first shown option is chosen. (T6) `make ask --clarified` without an id answers the latest open clarification of that question; the prompt says a clarification settles what it answered. Also `infra/pg-target/readonly_role.sql` (a password written as an identifier broke fresh seeds). Delivered in build `2026-11-17 · D68 (5A)` with 5A |
| D68 | new (owner, 2026-11-17; P5-1, P5-2, P5-3, P5-8, P5-10) | **Identity and roles.** Four Gaia roles: **user** (asks), **sme** (reviews the data sources assigned to them; also asks), **admin** (data sources, directory, roles and grants; also asks; never reviews) and **auditor** (audits the administrator's work from reports; asks only if also granted `user`). An admin cannot hold `sme` or `auditor` (separation of duties): a conflicting grant is not applied and is shown to the admin and in the auditor's report. Identity comes from the company directory — **Active Directory / LDAP(S) in 5A, Microsoft Entra ID (OIDC) in 5A.2**, behind one provider interface. The administrator chooses, per directory, how roles are read (group membership with nesting, a user attribute, or the OU), then grants Gaia roles to directory roles or to single users; a user without any Gaia role cannot sign in. **Exactly one local account** (`gaia-admin`), for setup and break-glass: its password is set by `make admin-user` or at its first sign-in with a one-time code that command prints; it is recovered **only** through that command (access to the server is the proof; no e-mail recovery — a mailbox would become the weakest link of the most powerful account); every local sign-in is flagged to the auditor. Sessions: server-side (P5-8); the directory is asked again every 15 minutes, so a disabled account loses access within that time |
| D69 | new (owner, 2026-11-17; P5-4, P5-5) | **Review and need-to-know.** The review item shows everything Gaia knows about a column, summarised, with a dive-in (meaning, alternatives, assumptions, unknowns, value domain, ledger, critique, profile within the exposure policy, tests, hints, lineage, versions, the questions that used it). Need-to-know as proposed (categories, all required, deny by default), **plus a PII-processor permission** (KVKK *veri işleyen*): a user without it sees the values of personal-data columns masked in every answer (live and stored); with it, unmasked. Masking is display, not access: whether a non-processor may also filter or group by a personal-data column is settled in 5C |
| D70 | new (owner, 2026-11-17; P5-6, P5-7) | **Governance.** The administrator evaluates and decides every access decision — who is an SME of what, who holds which Gaia role, who is a PII processor, which categories a role may read; Gaia proposes, the SME states what the data *is* (its category), the administrator decides who may *see* it. The **auditor** reviews those decisions from reports: the access matrix (who can see what, and through which directory role), the PII-processor list, every grant and revocation with who made it and when, directory and data-source changes, sign-ins (local ones flagged) and refusals — exportable, and from an append-only, hash-chained audit log. The SQL of an answer is visible to SMEs, admins and auditors (audit view), never to `user` (P4-2) |
| D71 | new (2026-11-17, Phase 5A.2; P5-1 timing by the assistant) | **Entra ID.** OpenID Connect, authorization code with PKCE, a confidential client (client secret, stored encrypted like D65's). The ID token must be RS256 and valid against the tenant's published keys, issuer, audience, expiry (±2 min), nonce and tenant. Who the person is: the token's `oid`. What they are: Microsoft Graph with the app's own credentials (`User.Read.All`, `GroupMember.Read.All`) — never token claims, which carry no names and stop at ~200 groups — so the 15-minute re-check and the sync work as for LDAP. Roles (P5-3): transitive groups by name prefix, security groups only by default, or one of five user attributes. A sign-in is single use, 10 minutes, bound to its browser by a SameSite=Lax cookie; the callback hands over to the app with a same-site navigation so the SameSite=Strict session cookie survives. A password never goes to an Entra directory. Built right after 5A, before 5A is verified on a real AD at the owner's request ("continue with the next step"), since nothing in it changes 5A |
| D72 | amends D67 (T6) | **The SQL prompt is D65's again, word for word.** Three golden runs after D68 (one on D68, two on D71) gave 90 % correct each, against 97 % on D65; the misses were mostly answers *after* a clarification (o02 twice, o03, o05), which D65's run never had, and the only change to what the model receives in between was T6's prompt sentence ("a clarification settles what it answered"). It is withdrawn, and the re-ask block is `Earlier clarifications:` as in D65. T6's actual fix — a `--clarified` follow-up carries its question — is code and stays. Checked in the sandbox: the messages for all 32 golden questions are byte-identical to build D65's on the same store; a test pins the system prompt's hash. Two fresh golden runs on the host decide it (OWNER_VERIFICATION §24b): back near 97 % → the sentence was the cause; still 90 % → run-to-run variation of the provider, and 90 % is the honest baseline. A prompt rule about clarifications comes back only with a measurement |
| D73 | new (2026-11-17, Phase 5B; D16, D69) | **SME review.** A decision — accept, correct (name, purpose, role, entity, unit, synonyms and **what each value of a code means**), reject (a note required) or not used — is a **new version** of the meaning, signed (`reviewed_by`, `reviewed_at`, `review_note`; migration `0014`), status `sme_reviewed` / `sme_rejected`, score 1.0 (`accepted`) or 0.0 (`abstain`); the machine's versions stay in the history (Phase 6 calibrates against these pairs). The ledger gets an `sme` item; the review item closes. **Carried forward:** re-reasoning keeps an SME's column version current while that column's structure hash is unchanged, and the table's SME purpose/grain while the table's is; otherwise the decision turns `stale` — still signed, no longer used, listed under "stale decisions" for a second look. **Lineage:** pass-through view columns inherit at once, a rejection included (a re-score of those views, no model call); computed view columns are queued (`input_reviewed`), never copied. Join hints the model proposed are confirmed or rejected (`source = sme`; the guard accepts a confirmed one; a declared FK is not reviewable). **Definitions reach the SQL generator:** value meanings next to a code column's values ("'2' = overdue", marked as confirmed by an expert) and glossary terms used by a question (or their synonyms) in a DEFINITIONS block — both only when they exist, so a store without SME work yields the same prompts as before. Only an SME of the data source may review it (403s audited); every decision and glossary change is in the audit chain. The SME sees personal data as shapes unless also a PII processor (D69). The object's **data category** moves to 5C, where categories exist |
| D74 | new (2026-11-17, Phase 5C; P5-5, P5-6, D69, D70) | **Need-to-know.** One data category per object (table or view), in `auth.object_category`: Gaia proposes it by matching the object's schema, name and meaning against each category's name and keywords (deterministic; a view reading one category's objects gets that category; a tie or no match proposes nothing), an SME confirms what the data is, the administrator may set it, and Gaia's proposals never replace a confirmed one. A person reads an object's columns only through a `category` grant (to a directory role or an account); an object without a category is readable by nobody; a view needs its own category and its columns' bases (pass-through: the base column; computed: all inputs; unknown lineage: everything its definition reads). **Personal data is governed by the PII-processor permission, not by a category** (the assessment's built-in "Personal data" category is superseded by D69): a non-processor may show a personal-data column (masked, in the live answer too) or count it or test `IS [NOT] NULL`, nothing else — filtering, joining, grouping, sorting or computing with it would reveal what the mask hides; a processor is not restricted. A column is personal data if a detector (≥ 80 %) or the semantic type says so, or an SME states it (an SME's "no" wins); a view column inherits through lineage. Stored answers keep personal data masked for everyone (minimisation). Enforced in retrieval (a restricted index) and in the guard (a restricted catalog; `*` refused; a reference that exists but is not the person's → `refused`, never repaired, never run, audited without naming it to the user). Every chat turn computes the policy afresh. `user` is in `auth.chat_roles`; the operator's command line keeps the full policy unless `--as LOGIN` |
| D75 | new (2026-11-17, Phase 5D, 5E; P4-2, P5-7, P5-12) | **Chat screen, the SQL, runs.** The chat streams each turn (stages, the table as soon as it exists, then the narrative), shows the confidence as a level with its reasons — never a number before Phase 6 — and clarifications as choices. The SQL of answers is in an audit view: SMEs see the queries of the data sources they review, administrators and auditors all; a person with only the `user` role never sees SQL, not even of their own answers (P4-2). `chat.query_run.asked_by` records the asker. The runs view is read-only, for administrators and auditors; starting, stopping, scheduling and switching models stay in Phase 7 (P5-12). 5C and 5D were built together at the owner's request, to be verified on the host in one run with a real AD/LDAP and Entra ID |
| D76 | new (2026-11-17, triage of the D75 run; P5-9, D68, D74) | **What the D75 run taught.** (a) *A directory's test proves its TLS*: the handshake the sign-in makes — LDAPS or StartTLS, with the trust the sign-in uses — is part of the test, and a refusal is explained from OpenSSL's verification code, the server's certificate and the CA Gaia trusts (`gaia.auth.tls`). (b) *A failing test of proven settings keeps the directory at sign-in*: `verified` means "these settings passed once" (any change resets it), so a later failure is shown (*last test failed*) and sign-ins fail for its cause, but one failed click no longer leaves only the local account. Settings never proven stay `failed`. (c) *A CA file is found the same way by every process* (relative to `backend/`, else the repository root) and the CA may live in the settings (`ca_pem`). (d) *Need-to-know before repair*: a statement that reaches for an existing object or column outside the person's categories is refused whatever else is wrong with it — never sent back for repair, audited. (e) *The schema's revision is checked*: `/health`, the API's start-up log and every command-line tool name a missing migration. (f) Profiling resolves data sources added at runtime like every other tool. (g) The local account has no Chat; a page that fails to render shows its error |
| D77 | new (2026-11-17, triage of the D75 run; D74, P5-6) | **Category proposals v2** (`gaia.access.proposal`; migration `0016`: `auth.object_category.basis`). Still deterministic and shown to people. Direct evidence: the category's words in the object's schema (5), name (2) or business name (2); weak: only in its entity (1) or purpose (½). Related evidence: the table it is named after (2.5) and the tables it references — declared FKs, joins an SME confirmed (1 each) — and, a little, direct-evidence tables referencing it (½); weighted by the neighbour's certainty (a person's confirmation 1.5, direct 1, related 0.7, weak 0.4); three rounds. Views: one category read → that category. Ties and no evidence propose nothing. Each proposal keeps its basis and reasons; the admin screen shows *only those to check*. Category vocabularies are literal words ("HR" is not "hour"). "Categories from the schemas" adds the first literal word of each table's name that no other schema uses, and skips `dbo`/`public` |
| D78 | **accepted and built** (owner, 2026-11-17; P5-3, D69; migration `0017`) | **An account is the directory's object, not its login.** An account is matched by directory and login, and its stable id (AD `objectGUID`, OpenLDAP `entryUUID`, Entra's object id — a DN never counts) must stay the same: when another object carries the login, the sign-in is refused and audited (`identity_changed`, once), an open session ends at its re-check, and sync leaves the account untouched, until an administrator **re-links** it (the same person: the account and its own grants follow) or **starts it anew** (someone else: its own grants revoked, sessions ended) — *Accounts*, or `make auth ARGS="account relink|renew DIR/LOGIN"` (`DIR --all` for a test domain provisioned again). A reused login never inherits its previous holder's grants, a PII-processor grant included |
| D79 | new (2026-11-17, triage of the D77 run) | **What the D77 run taught.** (a) An effect returns nothing but a clean-up function (newer browsers make `scrollIntoView()` return a Promise; the chat broke) — a test rejects any bare-expression effect. (b) Entra's "security groups only" excludes Microsoft 365 groups (`groupTypes` "Unified"), which the admin center makes security-enabled; the probe names every group it leaves out, and why. (c) Category proposals v2.1: a schema naming a category decides (8) unless a person says otherwise; a table whose primary key holds a foreign key belongs to that table (2.5); other references share one vote, as do the tables referencing one; a table named like an object of another schema or data source is about the same thing (2, from confirmed or direct objects only); an earlier proposal the rule no longer makes is kept but marked weak, with why. (d) `make share`: outputs with personal data replaced consistently, the key kept by the owner |
| D80 | new (2026-11-17, Phase 6A; P6-2) | **The evaluation store and the quality report.** Every golden run is recorded (`evaluation.run`, `evaluation.item`, migration `0018`) with its build, model, question set (by content) and mode — fresh, replay, mixed, imported; replays are never measurements, and a directory with recorded answers is refused unless `--resume`. `make quality` pools measured runs with 95 % Wilson intervals, names flaky questions, and compares sets of runs question by question (seeded paired bootstrap; P6-2: passes when the lower bound is above −3 points, fails when the upper is below, else cannot tell) |
| D81 | new (2026-11-17, triage of the D80 run; P5-7) | **What the D80 run taught.** (a) The SQL of answers is offered only where the person may see it — `/api/data-sources` says per data source; an SME sees their data sources' SQL, administrators and auditors all (the link had led SMEs elsewhere to "no such query"). (b) A golden run records a fingerprint of the **meanings** it answered with; a comparison of runs on different meanings says the difference is not the build's alone (the owner's SME decision had been credited to D80). (c) An evaluation item keeps its final SQL; the report shows it for questions wrong in every run. Migration `0019` |
| D82 | new (2026-11-17, Phase 6E) | **The replayed golden net.** In `make test`: every golden question through the whole pipeline with a scripted model that errs on purpose; prompt fingerprints (approximate numbers left out) and outcomes against a stored net; a moved prompt or outcome fails and is named; `make golden-net-accept` accepts an intended change, which for a prompt needs fresh golden runs (P6-2); a server whose data profiles differently keeps its own net |
| D83 | new (2026-11-17, Phase 6B; P6-3, P6-5) | **Labels from people, and the blind share.** An SME decision labels the machine meaning it judged — right, partly or wrong by the rule of `gaia.evaluation.human_labels` — with its model, prompt, scoring version and score; the LLM judge never labels (P6-0). 10 % of review items (`semantics.review_blind_share`) hide Gaia's certainty everywhere until decided, and the decision records it, so agreement with and without it can be compared |
| D84 | new (2026-11-18, triage of the D83 run) | **What the D83 run taught.** (a) The replayed net fingerprints a narration's result rows as a set: a query without `ORDER BY` returns rows in the server's order (`g08`, `g09` on the owner's host); an order that matters is still watched by the scorer. (b) An evaluation item keeps what its final answer was made of — the context's objects, the expected objects it lacked, interpretation, assumptions, the clarification given, the confidence shown (`evaluation.item.detail`, migration `0020`) — and the report splits wrong answers into **retrieval** (an expected object missing) and **reading** (all there), so a question like o06 says why it is wrong. (c) An AUROC on fewer than 10 labels of either kind is printed as no measurement. (d) A run's meanings are "unknown" because it was imported or recorded before D81 — the report says which |
| D85 | new (2026-11-18, Phase 6B part 2; P6-0) | **The owner's 3C audit as labels.** `make labels --import-3c DIR` keeps each audited answer once (`evaluation.label`): the owner's verdict, the answer's score, model and condition, and the judge's verdict beside it. The judge's agreement with people — share and Cohen's κ — is reported everywhere and read by nothing (no calibrator, no gate). Only production-mode conditions (N-P, N-P-raw) are calibrated on |
| D86 | new (2026-11-18, Phase 6C; P6-1, P6-3) | **Calibrators.** Per kind (meanings, answers), model and scoring version, fitted on people's labels pooled over data sources: isotonic from 150 labels with 30 not right, Platt from 75 with 15, else the identity; judged on held-out labels (five folds by subject) against the identity by ECE and Brier, with a reliability diagram; `make calibrate --apply` puts one in use only if it beats the identity on both (`--force` otherwise, logged here); at most one in use per kind, model and scoring version. A meaning calibrator in use turns stored scores into P(right) with a Wilson lower bound on `make rescore` (P6-3: accepted only with P ≥ 0.90 and bound ≥ 0.80); while none is, the stored inputs do not change. Every uncalibrated score says so on the screens |
| D87 | new (2026-11-18, Phase 6D part 1, 6E part 2; P6-2, P6-4) | **"Was this right?" and answer confidence.** Under every answer: right, partly, wrong, with a note; right is counted, partly and wrong go to the data source's SMEs (*Answers people doubted*), who decide what the answer was and why; only the decision is a label (with golden items, which keep the confidence shown). Once an answer calibrator is in use, the confidence is P(right) — high from 0.95, medium from 0.75 (`calibration.answer_*`) — shown as a percentage to SMEs, administrators and auditors and as a level to everyone else. `make golden --regress joins-rule` runs the SQL prompt without its JOINS rule, under its own build label, for the fresh net's acceptance test; a comparison prints repairs, calls and tokens per run |
| D88 | new (2026-11-18, Phase 6G) | **Category proposals measured.** Against what people confirmed: today's rule with each confirmation hidden in turn (leave-one-out, per basis, with the misses), and the proposals people saw before their first decision (kept or not). Both lean towards agreement (people decided with the proposal on screen) and the report says so. D77's weights are not fitted yet — not on ~50 confirmations |

## Definition of done (per phase)

A phase is done only when all of the following hold:

1. Every acceptance criterion is ticked, and each tick corresponds to a command
   that was actually run — not to code that merely exists
2. Tests cover the new logic, and the full suite passes
3. `OUTLINE.md` records the verified results and any defects found
4. A short summary states what was built, what was proven, and what was deferred
5. Your gate is ticked

## Open items

*Decided 2026-11-16: D52 with refinements (a)–(d); D56 (SQL Server asynchronously, on
delivered builds). Gates 3.0, 3A (PostgreSQL), 3B ticked; Phase 4 approved, 4A built.*

*2026-11-17: the D65 host runs triaged (Phase 4, "Owner runs on D65"); the owner's answers in
OWNER_VERIFICATION §21f. Phase 4 complete; Phase 5 approved (D68–D70); 5.0 and 5A built (build
D68); 5A.2 (D71); the D71 runs triaged, T6's prompt sentence withdrawn (D72); 5B built (D73).*

*2026-11-17 (later): D75 run → §26, build D77; D77 run → §27, Phase 6 approved, build D80;
D80 run → §28, build D83 (D81–D83).*

1. **Owner — the Phase 5 gate**: everything of Phase 5 is verified on your host (§28a).
2. **Owner — the next run: OWNER_VERIFICATION §28d** (build D83): migrate (`0019`) and test (the
   replayed net meets your server); the SQL link; GAIA-Finance in Entra; review with blind items
   and `make labels`; three fresh golden runs with their meanings; the *Evaluation* page; `make
   share`.
3. **Assistant — next:** triage of §28d; 6B part 2 (3C's labels, the judge's κ); 6C —
   calibration, once P6-1's label count is near (150 per model; `make labels` counts them).
4. **Owner, asynchronous:** the `demo_pg` 3C run; the SQL Server track S4, S8; `make
   evaluate-cost` (§13d). *(The 3C audit is done: 86.2 % of 94 — P6-0.)*
5. **Owner:** D61 (b) the soft clarification — accept, change or reject ((a), the 90 %
   threshold, accepted 2026-11-16). The golden runs' best guesses were the owner's answer in
   2–3 of 8–9, so asking stays right for now. D61 (c), definitions from an SME through the
   glossary, is built with 5B: §24d shows it at work on g07.
6. **Owner:** P4-1 for `nge_small` whenever ready; P4-5/6 — built on the recommendations
   (D65), say if either should change.

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
| Tune the 3A budget defaults from both dialects' data | Overtaken by D52: defaults stay; PostgreSQL numbers decide, SQL Server's inform (`dw.sales_wide` stays `partial`, labelled) |
| P4-7 (PostgreSQL in Phase 4) | Settled by D52 |
| `scripts/owner_runs.sh setup` (once) | Ran on D65 (`20_venv`, `20_migrate` — already at `0010`; the tests without the databases, §21 T8). Whether `GAIA_MASTER_KEY` is set shows in §20b |
| Former P5-1 / P5-2 (local accounts for reviewers; review granularity) | Superseded 2026-11-17 by the owner's directive (company directory, three roles, need-to-know): new P5-1 … P5-12 |
| The golden run of `demo_pg` (§18b) | Done on D65: executable 100 %, correct 97 % |
| Q1–Q3 of the triage (OWNER_VERIFICATION §21e) | Answered 2026-11-17: the two runs possibly overlapped (→ D67's lock either way); `nge_small` ends in 2016 (the empty answer was right); `deepseek-flash` is `deepseek-v4.1-flash` renamed (3C compares like with like) |
| B-P-nt's `http 400` (T4) | The old gateway's refusals; the calls succeeded on the new provider — B-P-nt complete |
| Phase 5 approval, P5-1 … P5-12 | Approved 2026-11-17 (D68–D70) |
