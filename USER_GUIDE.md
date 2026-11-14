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

Three stages exist and work:

| Stage | Command | What it produces |
|---|---|---|
| **Introspect** | `make introspect` | A deterministic JSON snapshot of the target schema, plus normalized catalog tables |
| **Reason** | `make reason` | A business meaning per table and column, with evidence, alternatives and an honest score |
| **Embed** | (inside `make reason`) | 1024-dimension vectors for retrieval in Phase 4 |

Targets: SQL Server (`demo_mssql`, the default) and PostgreSQL (`demo_pg`, a
synthetic development target). Every stage works on both; choose with
`TARGET=<name>` in `make`, or `--target <name>` on the tools.

Not built yet: profiling (Phase 3, approved), SQL generation (Phase 4), the web
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
make check-target             # 7/7 checks against the target
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

# 2. Preview what reasoning would do — no LLM calls, no writes
make reason-dry ARGS="--table dbo.transactions --with-neighbours"

# 3. Reason for real
make reason ARGS="--table dbo.transactions --with-neighbours --cost-report"

# 4. Run it again — should make zero LLM calls
make reason ARGS="--table dbo.transactions --with-neighbours"
```

Step 4 is the incremental guarantee: any table whose structure hash, prompt
versions and model are unchanged is skipped, so a repeat run costs nothing.

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

Exit codes: `0` success, `1` aborted, `2` some tables or critiques failed (their
raw payloads are in `audit.llm_call`).

### Stack

| Command | Does |
|---|---|
| `make up` / `make down` | Start / stop metadata DB + Redis |
| `make reset-db` | **Destroys** the metadata DB and re-initialises it |
| `make psql` / `make redis-cli` | Shells into the containers |
| `make test` | Full test suite |
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

---

## 10. Known limits

State these honestly when showing Gaia to anyone:

| Limit | Consequence |
|---|---|
| **No column comments in the target** | Meaning comes from names, types and keys only; scores cap near 0.70 |
| **No profiling yet (Phase 3)** | Nothing reaches `accepted`. Even with its `profile` share, Phase 2 scoring caps an uncommented plain column at 0.775; Phase 3B replaces the scoring |
| **Slow LLM provider** | A wide bundle can take ~12 min; `llm.timeout_s` is 1800 s and read timeouts retry once. Re-assessed on provider change |
| **`thinking` is not in the skip key** | Toggling it does not upgrade existing answers without `--force`; the default path always uses thinking |
| **Targets come from config** | Several targets can be configured and selected with `--target`; admin-supplied connections are a Phase 4 item (OUTLINE §24) |
| **No question answering yet** | Phases 4–5 build SQL generation and the chat UI |
| **No SME review UI yet** | Low-confidence items are stored and flagged, but reviewed by hand in SQL; the review UI moves into Phase 5 (R1) |
| **Two dialects** | SQL Server and PostgreSQL (catalog, read-only probe, reasoning). Profiling is built for both in Phase 3A; further engines stay in Phase 7 |
| **SQL Server statement timeout not yet applied** | Configured but not enforced on SQL Server sessions until Phase 3.0; PostgreSQL sessions enforce it |

### What "good" looks like today

On `nge_small`, from zero comments:

* `line_tax` → "Line Tax Amount"
* `quantity` → "Quantity Sold"
* `unit_price` → "Unit Price"
* `condition` → "Item Condition"

Correct, useful, and correctly *not* trusted — each scores in the
`deep_profile` band, because the catalog alone cannot prove them. That is the
system working: it knows what it does not know, and says so with a number.

