# Gaia — User Guide

A practical, task-oriented guide to running Gaia. It assumes the design is
already agreed (see [OUTLINE.md](OUTLINE.md)) and that you want to *use* the
thing.

Every SQL Server command here has been run against the real stack: PostgreSQL
18 + pgvector, SQL Server 2025 (`nge_small`), the DeepSeek gateway, and Ollama.
The PostgreSQL-target commands (§3.1) were verified in a sandbox on PostgreSQL 16;
the Compose service itself is verified on first use on the real host.

---

## Contents

1. [What Gaia does today](#1-what-gaia-does-today)
2. [Prerequisites](#2-prerequisites)
3. [First-time setup](#3-first-time-setup)
4. [The daily workflow](#4-the-daily-workflow)
5. [Command reference](#5-command-reference)
6. [Reading the output](#6-reading-the-output)
7. [Inspecting the results yourself](#7-inspecting-the-results-yourself)
8. [Configuration you are likely to change](#8-configuration-you-are-likely-to-change)
9. [Troubleshooting](#9-troubleshooting)
10. [Known limits](#10-known-limits)

---

## 1. What Gaia does today

These stages exist and work:

| Stage | Command | What it produces |
|---|---|---|
| **Introspect** | `make introspect` | A deterministic JSON snapshot of the target schema, plus normalized catalog tables |
| **Profile** | `make profile` | Evidence from the data (Phase 3A): column profiles, value-type detections, cross-column identities, view lineage and aliases, join candidates; `make profile-tests` later tests the model's claims |
| **Reason** | `make reason` | A business meaning per table and column, with evidence, alternatives and an honest score (Phase 3B: scored from an evidence ledger) |
| **Review queue** | `make review` | What the SME should look at first, and why |
| **Embed** | (inside `make reason`) | 1024-dimension vectors for retrieval in Phase 4 |

Targets: SQL Server (`demo_mssql`, the default) and PostgreSQL (`demo_pg`, a
synthetic development target). Every stage works on both; choose with
`TARGET=<name>` in `make`, or `--target <name>` on the tools.

Not built yet: the Phase 3 evaluation (3C), SQL generation (Phase 4), the web
UI for SME review and chat (Phase 5).

The important consequence: **Gaia does not answer questions yet.** It builds the
semantic understanding that will let it answer questions.

---

## 2. Prerequisites

| Requirement | Check |
|---|---|
| Docker + Compose | `docker compose version` |
| Python 3.12 venv at `gaia-env` | `./gaia-env/bin/python --version` |
| ODBC Driver 18 for SQL Server (SQL Server targets only) | `odbcinst -q -d` |
| SQL Server container `sql1` with the target restored | `docker ps --filter name=sql1` |
| PostgreSQL dev target (optional) | `make pg-target-up` |
| Ollama container `ollama-code` with `bge-m3` | `docker exec ollama-code ollama list` |
| Gateway API key in `.env` | see §8 |

Gaia never creates or modifies a real target database. The database and the
read-only login are provisioned by you once: restore as in OUTLINE §4.1, then run
[`infra/sqlserver/001_readonly_login.sql`](infra/sqlserver/001_readonly_login.sql).
The only exception is the synthetic PostgreSQL development target, which Gaia's
fixture script creates (§3.1).

---

## 3. First-time setup

```bash
cd /home/murat/g120s/gaia

cp .env.example .env          # then fill in real secrets
make up                       # starts metadata PostgreSQL + Redis
make verify-extensions        # expect 6 extensions
make check-env                # imports + ODBC drivers
make check-target             # 9 checks against the target (read-only proven twice)
make check-embed              # bge-m3, 1024 dims
make check-llm                # plain, JSON mode, thinking

cd backend && ../gaia-env/bin/alembic upgrade head && cd ..
```

`make check-target` is expected to **fail** if the login can write. That is the
check doing its job, not a setup error.

### 3.1 Optional: the PostgreSQL development target

```bash
# in .env: GAIA_PG_TARGET_PASSWORD (read-only role) and
#          GAIA_PG_TARGET_ADMIN_PASSWORD + GAIA_PG_TARGET_ADMIN_URL (container owner)
make pg-target-up                      # postgres:18 on 127.0.0.1:5434
make pg-target-seed                    # schema + ~3 M rows + read-only role, ~1.5 min
make check-target TARGET=demo_pg       # expect 9 PASS
make introspect   TARGET=demo_pg       # 28 objects, 213 columns
```

`pg-target-seed` is deterministic (`--seed 42`) and re-runnable: it drops and
recreates the fixture schemas. `ARGS="--scale 0.1"` gives a quick 0.4 M-row
version; `--scale 5` a larger one for profiling budgets. The worst-case tables are
in schema `legacy`; `rpt.v_legacy_transactions` is the only place their business
names appear (as view aliases).

For PostgreSQL, read-only is proven from **privileges**: the role owns nothing,
has no write or CREATE privilege and no bypassing attribute, and a DDL attempt in
an explicit `READ WRITE` transaction fails with a permission error.
`default_transaction_read_only` is not accepted as proof — any session can
switch it off.

---

## 4. The daily workflow

```bash
# 1. Capture the schema (cheap, always safe, always full)
make introspect

# 2. Collect evidence from the data (bounded reads; no LLM)
make profile-dry ARGS="--table dbo.transactions --with-neighbours"   # the plan, no reads
make profile     ARGS="--table dbo.transactions --with-neighbours"

# 3. Preview what reasoning would do — no LLM calls, no writes
make reason-dry ARGS="--table dbo.transactions --with-neighbours"

# 4. Reason for real
make reason ARGS="--table dbo.transactions --with-neighbours --cost-report"

# 5. Test what the model claimed, then re-reason what changed
make profile-tests ARGS="--table dbo.transactions --with-neighbours"
make reason        ARGS="--table dbo.transactions --with-neighbours"

# 6. Run both again — zero data queries, zero LLM calls
make profile ARGS="--table dbo.transactions --with-neighbours"
make reason  ARGS="--table dbo.transactions --with-neighbours"
```

Step 6 is the incremental guarantee: a profile is reused while the table's
structure and row count (±20 %) are unchanged and it is younger than 30 days;
a table whose structure hash, prompt versions, model and evidence hash are
unchanged is not reasoned again. Re-introspect first when the database has
changed: freshness compares against the latest snapshot.

### Growing the scope

```bash
make reason ARGS="--schema dbo --limit 5"    # try a slice first
make reason ARGS="--cost-report"             # everything captured
```

Start narrow. Reasoning is the slow, paid step; introspection is not.

---

## 5. Command reference

### Introspection

All target-facing commands accept `TARGET=<name>`.

| Command | Does |
|---|---|
| `make introspect` | Read the catalog, diff against the last snapshot, persist |
| `make introspect-dry` | Same, without writing |
| `make introspect-json` | Write the snapshot document to `/tmp/gaia-snapshot.json` |

Introspection always re-reads the full catalog. It costs seconds, and skipping
the read would risk missing comment edits, which `sys.objects.modify_date` does
not expose.

### Reasoning

| Option | Effect |
|---|---|
| `--target NAME` | Data source from config (default `app.default_target`; `TARGET=` in make) |
| `--table dbo.x` | Limit to one table (repeatable) |
| `--with-neighbours` | Add tables linked by a declared FK, **one hop only** |
| `--schema dbo` | Limit to a schema (repeatable) |
| `--limit N` | At most N tables, after sorting by name |
| `--dry-run` | No LLM calls, no writes |
| `--show-bundles` | With `--dry-run`: print the exact prompt per table |
| `--no-thinking` | Disable thinking for semantics and critique |
| `--force` | Re-reason even if unchanged (reuses cached responses) |
| `--no-cache` | Bypass the Redis response cache |
| `--no-embed` | Skip the embedding step |
| `--cost-report` | Print every LLM call with tokens and latency |
| `--evidence-file F` | Evidence as JSON (`gaia.profiling.summary` format) instead of the profile store — fixtures and experiments |
| `--replay DIR` | Answer from files instead of a provider (`DIR/requests` → `DIR/responses`); recorded as model `replay` |

Exit codes: `0` success, `1` aborted, `2` some tables or critiques failed (their
raw payloads are in `audit.llm_call`).

### Profiling (`make profile`, `make profile-tests`)

| Option | Effect |
|---|---|
| `--target`, `--table`, `--schema`, `--with-neighbours`, `--limit` | Scope, as for reasoning |
| `--stage 3` | Test the stored claims (`make profile-tests`): inclusion per join hint, exact uniqueness for identifiers, exact value lists for codes and flags whose counts were sampled |
| `--dry-run` | The plan (profile / reuse / skip, or the tests) without reading the target |
| `--force` | Re-profile fresh tables; with `--stage 3`, re-run tests of claims |
| `--ignore-window` | Run outside `profiling.window` / `targets.<name>.profiling_window` |
| `--no-joins` | Stage 1 without join discovery |
| `--report` | List every join candidate tested, not only those kept |
| `--json` | Machine-readable report |

What a run prints, per table: `profiled` / `partial` (some metric degraded —
the reason is stored with it) / `reused` / `skipped (reason)`, the size class,
queries, estimated pages and time; then module and view-lineage counts, data
queries vs catalog queries, join candidates kept, and the **footprint**:
statistics that appeared on the target during the run (SQL Server creates them
for columns it had none for; PostgreSQL never does).

Skip reasons: `view` (views are never sampled; their definitions become lineage),
`catalog_sufficient` (every column already has a comment, key, trusted FK or
check), `no_select`, `budget`, `window`, `privilege_check_failed`. The first
three are final for reasoning (coverage cannot grow by profiling); the others
are retried by the next run.

Safety: only `SELECT`; read-only session; statement timeout
(`profiling.budget.statement_timeout_s`); MAXDOP / parallel workers; page
budgets per table and per run; a per-table time ceiling no statement can
outlive; one run per data source at a time. Sample rows stay in memory; only
aggregates and at most five example values per column are stored.

### Measuring profiling cost (`make measure`)

Phase 3 budgets are set from measurements on the real target, not from
estimates. One unattended run per target:

```bash
docker restart sql1                       # SQL Server: empty the cache first (cold run)
make measure TARGET=demo_mssql ARGS="--table dw.sales_wide --table dw.fact_sales"
make measure TARGET=demo_pg               # PostgreSQL dev target; tables chosen automatically
```

Without `--table` it picks the two largest tables and the widest table with at
least 1 M rows. Per table it measures a count, one batched min/max scan, an
exact and an approximate distinct count for every column, average/stddev native
vs float, exact/approximate/sampled median, row-sized samples and exact top-k —
each with milliseconds and **pages**, the headline ones twice (cold, then warm).
It also records auto-created statistics (SQL Server) and `TABLESAMPLE` support.

Options: `--statement-timeout` (300 s per statement), `--max-minutes` (90,
total budget; later measurements are skipped, not the run), `--maxdop` (2),
`--max-columns` (60), `--sample-rows` (10,000), `--out`. It prints a Markdown
report and writes it with a JSON twin to the temp directory. Only `SELECT`,
`SET` and `EXPLAIN` are issued; sampled rows are counted, never stored. On SQL
Server the exact distinct counts make the engine create statistics — the report
lists each one; that footprint is one of the things being measured.

### Stack

| Command | Does |
|---|---|
| `make up` / `make down` | Start / stop metadata DB + Redis |
| `make reset-db` | **Destroys** the metadata DB and re-initialises it |
| `make psql` / `make redis-cli` | Shells into the containers |
| `make test` | Full test suite (`ARGS="tests/... -k name"` for a subset) |
| `make test-db` | Create the disposable `gaia_test` database for the integration tests |
| `make measure` | Profiling-cost measurements for Phase 3 budgets (`TARGET=`, `ARGS=`) |
| `make dev-api` | FastAPI on `127.0.0.1:8000` (`/health`, `/api/meta`) |
| `make pg-target-up` / `pg-target-down` | Start / stop the PostgreSQL development target |
| `make pg-target-seed` | (Re)create and populate it (`ARGS="--scale 0.1"`) |
| `make pg-target-psql` | psql into it as the read-only role |
| `make pg-target-reset` | **Destroys** its volume |

---

## 6. Reading the output

A reasoning run prints a header, one line per table, then totals.

```text
data source    : demo_mssql
snapshot       : 66316d12-c53f-4890-b6bc-a0650d975c01
model          : deepseek-v4.1-flash
prompts        : reason_table.v1+2541a66663 / critique_table.v1+5fc0752dc5
thinking       : semantics=True critique=True

  dbo.customers           skipped       (unchanged since v1)
  dbo.transactions        reasoned      18 cols  mean=0.612

tables         : reasoned=1 skipped=0 failed=0
llm            : calls=2 cache_hits=0 tokens in=8728 out=10806 reasoning=3213
wall time      : 718.5s (sum of call latency 692.3s)
embeddings     : written=19 cache_hits=0
join hints     : fk={'upserted': 93, 'staled': 0} inferred=0

score distribution over 18 current columns in scope:
mean=0.612 median=0.621 min=0.467 max=0.702
bands: {'accepted': 0, 'deep_profile': 12, 'deep_profile_flagged': 6, 'abstain': 0}
```

Since Phase 3B the header also shows `generation : 2 (evidence, heuristic-v2);
values exposed as masked|raw`, and the totals add `coverage` (the deepest evidence
stage per column: catalog, modules, light_profile, targeted, sme) and `review
queue` (items queued by reason: abstain, evidence_exhausted, contradiction,
critique_disagrees).

### Interpreting a score

Bands (from `profiling` in `config/gaia.yaml`):

| Band | Score | Meaning |
|---|---|---|
| accepted | ≥ 0.80 | Well supported |
| deep_profile | 0.60–0.80 | Plausible but under-evidenced → Phase 3 work |
| deep_profile_flagged | 0.45–0.60 | Weak; profiling plus human review later |
| abstain | < 0.45 | Not trustworthy; queued for SME |

**Do not treat a low average as failure.** On a schema with no column comments,
`evidence_coverage` cannot exceed ~0.10, which caps scores near 0.70 by
construction. A uniformly high score on such a schema would mean the scorer is
lying.

This is the Phase 2 scoring (`heuristic-v1`). Phase 3B replaces it with
`heuristic-v2` (PHASES P3-2): evidence that *supports* a meaning raises the
score, evidence that *contradicts* it lowers it, and names alone can never reach
`accepted`. Phase 4 retrieval will use every non-abstain meaning, ranked by score
(roadmap R2), so `accepted` is not a precondition for being used.


---

## 7. Inspecting the results yourself

Everything is plain SQL against the metadata store.

```bash
make psql
```

### What was reasoned

```sql
SELECT t.schema_name, t.object_name, count(*) AS columns,
       round(avg(c.raw_score)::numeric, 3) AS mean_score
FROM semantics.column_semantic c
JOIN catalog.tables t ON t.id = c.table_id
WHERE c.is_current
GROUP BY 1, 2
ORDER BY 2;
```

### The meaning of one column, with its evidence

```sql
SELECT k.column_name, c.business_name, c.purpose, c.role,
       c.unit, c.value_domain, c.raw_score, c.band
FROM semantics.column_semantic c
JOIN catalog.columns k ON k.id = c.column_id
JOIN catalog.tables  t ON t.id = c.table_id
WHERE c.is_current AND t.object_name = 'transaction_lines'
ORDER BY c.raw_score;
```

### Why a score is what it is

```sql
SELECT jsonb_pretty(score_breakdown)
FROM semantics.column_semantic
WHERE is_current
LIMIT 1;
```

Each component shows its `value`, `weight`, `contribution` and the signals
behind it. This is the audit trail: no number is unexplained.

### Verify the scoring arithmetic

```sql
SELECT count(*) AS total,
       count(*) FILTER (
         WHERE abs((score_breakdown->>'raw_score')::float8 - raw_score) < 1e-6
       ) AS breakdown_matches
FROM semantics.column_semantic
WHERE is_current;
```

`total = breakdown_matches` always. If it does not, something is wrong.

### The evidence behind one column (Phase 3B)

```sql
SELECT e.direction, e.kind, e.strength, e.source, e.ref, e.detail
FROM semantics.evidence_item e
JOIN semantics.column_semantic c ON c.id = e.column_semantic_id
JOIN catalog.columns k ON k.id = c.column_id
WHERE c.is_current AND k.column_name = 'f7'
ORDER BY e.direction DESC, e.strength DESC;
```

### The data profile behind one column (Phase 3A)

```sql
SELECT cp.column_name, jsonb_pretty(cp.summary) AS profile,
       jsonb_pretty(cp.metrics) AS how_each_number_was_obtained
FROM profiling.column_profile cp
JOIN profiling.table_profile tp ON tp.id = cp.table_profile_id
JOIN catalog.tables t ON t.id = tp.table_id
WHERE tp.is_current AND t.object_name = 'tb_trx02' AND cp.column_name = 'f2';

-- tests (join candidates from Stage 1, tests of claims from Stage 3)
SELECT t.object_name, v.column_name, v.test_id, v.origin, v.passed, v.metric, v.detail
FROM profiling.verification_test v JOIN catalog.tables t ON t.id = v.table_id
WHERE v.is_current ORDER BY 1, 2;

-- view lineage
SELECT view_schema, view_name, column_name, classification, source, expression
FROM profiling.view_column_lineage ORDER BY 1, 2, 3;
```

### The review queue

```sql
SELECT t.schema_name, t.object_name, k.column_name, r.priority, r.score,
       r.coverage, r.reasons, c.business_name
FROM semantics.review_item r
JOIN catalog.tables t ON t.id = r.table_id
JOIN catalog.columns k ON k.id = r.column_id
JOIN semantics.column_semantic c ON c.id = r.column_semantic_id
WHERE r.status = 'open'
ORDER BY r.priority DESC;
```

Or `make review`. An SME decision (Phase 5) closes an item; a newer scored
version supersedes an open one, so the queue never shows a stale score.

### Failed calls

```sql
SELECT t.object_name, a.purpose, a.error, a.parsed_ok,
       a.finish_reason, left(a.raw_response, 200) AS raw
FROM audit.llm_call a
LEFT JOIN catalog.tables t ON t.id = a.table_id
WHERE NOT a.parsed_ok OR a.error IS NOT NULL
ORDER BY a.created_at DESC;
```

Note: `audit.llm_call` stores `table_id`, not a table name, and the HTTP status
is folded into `error`. Raw payloads are retained on every call, so a
non-JSON-mode failure can always be reconstructed.

### Join hints

```sql
SELECT source, status, from_table, from_columns, to_table, to_columns,
       confidence, rationale
FROM semantics.join_hint
WHERE source <> 'fk';
```

Declared FKs are `source='fk'`. Model suggestions are `source='llm'` and are
kept only if every column and target resolve in the catalog and they do not
duplicate a declared FK. Expect low confidence when no FK backs the guess.

### Run history and cost

```sql
SELECT started_at, status, thinking, tables_reasoned, tables_skipped,
       tables_failed, llm_calls, prompt_tokens, completion_tokens, wall_ms/1000 AS seconds
FROM semantics.reasoning_run
ORDER BY started_at DESC
LIMIT 5;
```

---

## 8. Configuration you are likely to change

All in `config/gaia.yaml`; secrets stay in `.env`.

| Setting | Default | When to change |
|---|---|---|
| `llm.concurrency` | 3 | **Never raise above 3.** The gateway caps at 4 per model; a retry needs a free slot |
| `llm.timeout_s` | 1800 | Read timeout per completion. Sized for the widest bundle plus thinking-mode headroom |
| `llm.thinking.semantics` / `.critique` | true | Turn off to trade depth for speed (~4–5× faster) |
| `llm.retry.max_attempts_rate_limit` | 8 | Raise only if the gateway gets flakier |
| `llm.max_output_tokens` | 16000 | Raise if you see truncation errors on wide tables |
| `scoring.weights` | 0.30/0.25/0.20/0.15/0.10 | Must sum to 1.0; changing them changes every score |
| `profiling.accept_at` | 0.80 | Lower to accept more columns; raises risk |
| `semantics.prompt_major` | 2 | 1 = Phase 2 prompts (catalog only); changing it re-reasons every table once |
| `scoring.version` | heuristic-v2 | heuristic-v1 = Phase 2 scoring |
| `scoring.v2.strengths.*` | see file | Evidence strengths; placeholders until Phase 6 calibration |
| `llm.locality` | remote | `local` sends data values raw; `remote` sends personal/free-text values only as shapes |
| `llm.context_budget_tokens` | 32000 | Prompts above it are split into column chunks (small-context local models) |
| `profiling.enabled` | true | Off (or `targets.<name>.profiling_enabled: false`) = catalog evidence is final, low columns go to review |
| `profiling.window` / `targets.<name>.profiling_window` | none | `"22:00-06:00"`: profile only after the target's statistics maintenance |
| `profiling.budget.full_scan_max_pages` | 12800 | Tables up to ~100 MB are profiled exactly (one `GROUP BY` per column); lower it if profiling reads too much |
| `profiling.budget.max_runtime_per_table_s` / `statement_timeout_s` | 180 / 60 | Per-table ceiling and per-statement timeout |
| `profiling.budget.max_pages_per_table` / `max_pages_per_run` | 2 M / 50 M | Estimated logical pages (8 KB) |
| `profiling.row_count_delta_ratio` / `profile_max_age_days` | 0.20 / 30 | When a stored profile is stale |
| `embeddings.model` / `dimensions` | `bge-m3` / 1024 | Changing either requires a re-embed and a `vector(N)` change |

`config/gaia.yaml` contains `${VAR}` placeholders resolved from `.env` at load
time, or `${VAR:-default}` where a non-secret default is fine (used for the
PostgreSQL target's host, port, database and user). Unresolved names are reported
by `/health` and `/api/meta` rather than failing opaquely; `check-target` only
counts those of the target being checked.

### Secrets

Only `.env`, never `config/gaia.yaml`, never the database. `.env` is
git-ignored. If a key is missing, `make reason` fails with a clear message;
`make reason-dry` still works.


---

## 9. Troubleshooting

### `Login timeout expired (HYT00)`

The host resolved to IPv6. The SQL Server container publishes IPv4 only.

```bash
grep GAIA_TARGET_HOST .env      # must be 127.0.0.1, never localhost
```

### `Cannot open database ... requested by the login`

Three causes, in order of likelihood:

1. **Orphaned database user** after the login was recreated — SIDs no longer
   match. Fix: `ALTER USER [gaia_ro] WITH LOGIN = [gaia_ro];`
2. **`DENY CONTROL` on the user.** `CONTROL` implies `CONNECT`, so a
   `DENY CONTROL` silently locks the login out entirely (Msg 916).
3. Missing `CONNECT` grant.

`make check-target` distinguishes these; read its per-check output.

### `http 429` / `http 503` from the LLM gateway

The gateway has a **hard cap of 4 concurrent requests per model** and returns
Turkish text saying so. The client retries transient capacity errors 8 times
with capped exponential backoff plus jitter.

If you see persistent 429s:

* confirm `llm.concurrency` is 3 or lower
* check nothing else is using the same API key in parallel
* check the remaining-quota headers on any successful call

### A wide table times out

The read timeout is 1800 s, which covers the widest bundle observed (~718 s)
with thinking-mode headroom. If it still trips, the table is wider than the
provider can handle in one call:

```bash
make reason ARGS="--table dbo.transactions --no-thinking"
```

You will see a distinct error rather than a generic failure, so a slow provider
is not mistaken for an unhealthy gateway. Read timeouts retry only once
(`retry.max_attempts_timeout: 2`), because each attempt already costs the full
timeout.

### SQL Server: `security.privileges` or `security.read_only` fails (since 3.0)

`security.privileges` names exactly what makes the login writable: `sysadmin`,
a writer role (`db_datawriter`, `db_ddladmin`, …), a database permission that is
not read-type (anything but `CONNECT`, `SELECT`, `SHOWPLAN`, `UNMASK`, `VIEW …`),
write/alter grants on tables, `ALTER` on a schema, or `EXECUTE` on a procedure
(procedures can write through ownership chaining). Remove the grant, or tell us
if a listed permission is genuinely read-only and should be allowed.

`security.read_only: inconclusive` means the DDL attempt failed for a reason
other than permissions (a timeout, a syntax error, a dropped connection). Before
3.0 any error counted as proof; now only permission errors do.

### `StatementTimeout` / `statement timed out`

Every target session enforces `limits.statement_timeout_s` (30 s) since 3.0 —
on SQL Server it was configured but not applied before. Catalog reads take
seconds; if a very large catalog ever exceeds it, raise the limit for that
target in `config/gaia.yaml`.

### PostgreSQL target: `security.privileges` or `security.read_only` fails

The role can write somewhere. The detail lists exactly what: role attributes,
owned relations, write grants, sequence `nextval` rights, `CREATE` on a schema or
the database. Re-run `infra/pg-target/readonly_role.sql` (or `make
pg-target-seed`) and never grant the role ownership. The live DDL attempt aims at
a schema where `CREATE` is granted if there is one, so a role that is safe in
`public` but writable elsewhere still fails.

### PostgreSQL target: `set GAIA_PG_TARGET_ADMIN_URL`

`make pg-target-seed` needs the container owner's DSN (the seed creates the
schema, data and read-only role). Gaia itself only ever uses the read-only role.
Passwords may contain any character; quote them in `.env` if they contain `;`,
`#` or spaces.

### `blank content returned`

JSON mode occasionally returns whitespace only. The client retries
automatically (`llm.retry.retry_on_blank_content`). If it persists, the prompt is
likely producing output that is not valid JSON — check
`audit.llm_call.raw_response`.

### Nothing is being reasoned (`would_reason=0`)

The table is unchanged since its last reasoning. That is the incremental
feature. To force a re-run:

```bash
make reason ARGS="--table dbo.x --force"
make reason ARGS="--table dbo.x --force --no-cache"   # fresh model answers
```

### Embeddings fail but reasoning succeeded

Ollama is a separate dependency:

```bash
docker exec ollama-code ollama list        # is bge-m3 there?
make check-embed
```

Semantics are already persisted; you can backfill embeddings later by re-running
without `--no-embed`.

### `make profile`: `REFUSED: outside the profiling window`

`profiling.window` (or the target's `profiling_window`) does not include the
current time. Run later, or add `ARGS="--ignore-window"` if you know the target
can take it now.

### `make profile`: `another profiling run holds the lock for this data source`

Only one profiling run per data source at a time. Wait for the other run; a
crashed run releases the lock when its connection closes.

### Views show `unresolved` lineage: `no definition`

SQL Server returns view definitions only to logins with `VIEW DEFINITION`
(granted by `infra/sqlserver/001_readonly_login.sql`). Without it, views are
reasoned from their columns only.

### A table is `partial`

Some metric degraded (budget, time ceiling, a rejected or timed-out statement);
the reason is stored in `profiling.column_profile.metrics` / `table_profile.notes`
and printed in the run. The rest of the profile is used.

---

## 10. Known limits

State these honestly when showing Gaia to anyone:

| Limit | Consequence |
|---|---|
| **No column comments in the target** | Meaning comes from names, types and keys only; scores cap near 0.70 |
| **Names alone never accept** | Without data evidence (no `SELECT`, profiling off, views) a column caps at 0.65 by design and goes to the review queue |
| **Integer references without FKs** | A column of integers pointing to a dense surrogate key cannot be confirmed from data (every integer in range is found); such joins rest on aliases, names and the model, and stay in review |
| **Profiling is bounded, not free** | Small tables (≤ ~100 MB) are read once per column for exact distinct counts; the run reports pages and time. Not built: statistics histograms, fan-out tests, profiling of computed view columns |
| **Slow LLM provider** | A wide bundle can take ~12 min; `llm.timeout_s` is 1800 s and read timeouts retry once. Re-assessed on provider change |
| **`thinking` is not in the skip key** | Toggling it does not upgrade existing answers without `--force`; the default path always uses thinking |
| **Targets come from config** | Several targets can be configured and selected with `--target`; admin-supplied connections are a Phase 4 item (OUTLINE §24) |
| **No question answering yet** | Phases 4–5 build SQL generation and the chat UI |
| **No SME review UI yet** | Low-confidence items are stored and flagged, but reviewed by hand in SQL; the review UI moves into Phase 5 (R1) |
| **Two dialects** | SQL Server and PostgreSQL (catalog, read-only probe, profiling, reasoning). SQL Server profiling awaits its first run on the owner's host; further engines stay in Phase 7 |

### What "good" looks like today

On `nge_small`, from zero comments:

* `line_tax` → "Line Tax Amount"
* `quantity` → "Quantity Sold"
* `unit_price` → "Unit Price"
* `condition` → "Item Condition"

Correct, useful, and correctly *not* trusted — each scores in the
`deep_profile` band, because the catalog alone cannot prove them. That is the
system working: it knows what it does not know, and says so with a number.

