# Gaia — User Guide

A practical, task-oriented guide to running Gaia. It assumes the design is
already agreed (see [OUTLINE.md](OUTLINE.md)) and that you want to *use* the
thing.

Every command here has been run against the real stack: PostgreSQL 18 +
pgvector, SQL Server 2025 (`nge_small`), the DeepSeek gateway, and Ollama.

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

Not built yet: profiling (Phase 3), SQL generation (Phase 4), chat UI (Phase 5).

The important consequence: **Gaia does not answer questions yet.** It builds the
semantic understanding that will let it answer questions.

---

## 2. Prerequisites

| Requirement | Check |
|---|---|
| Docker + Compose | `docker compose version` |
| Python 3.12 venv at `gaia-env` | `./gaia-env/bin/python --version` |
| ODBC Driver 18 for SQL Server | `odbcinst -q -d` |
| SQL Server container `sql1` with the target restored | `docker ps --filter name=sql1` |
| Ollama container `ollama-code` with `bge-m3` | `docker exec ollama-code ollama list` |
| Gateway API key in `.env` | see §8 |

Gaia never creates or modifies the target database. The database and the
read-only login are provisioned by you once; see
[`infra/sqlserver/RESTORE.md`](infra/sqlserver/RESTORE.md).

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
| accepted | ≥ 0.80 | Well supported; safe for retrieval |
| deep_profile | 0.60–0.80 | Plausible but under-evidenced → Phase 3 work |
| deep_profile_flagged | 0.45–0.60 | Weak; profiling plus human review later |
| abstain | < 0.45 | Not trustworthy; queued for SME |

**Do not treat a low average as failure.** On a schema with no column comments,
`evidence_coverage` cannot exceed ~0.10, which caps scores near 0.70 by
construction. A uniformly high score on such a schema would mean the scorer is
lying.


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
| `llm.timeout_s` | 600 | Raise if wide tables time out; see §10 |
| `llm.thinking.semantics` / `.critique` | true | Turn off to trade depth for speed (~4–5× faster) |
| `llm.retry.max_attempts_rate_limit` | 8 | Raise only if the gateway gets flakier |
| `llm.max_output_tokens` | 16000 | Raise if you see truncation errors on wide tables |
| `scoring.weights` | 0.30/0.25/0.20/0.15/0.10 | Must sum to 1.0; changing them changes every score |
| `profiling.accept_at` | 0.80 | Lower to accept more columns; raises risk |
| `embeddings.model` / `dimensions` | `bge-m3` / 1024 | Changing either requires a re-embed and a `vector(N)` change |

`config/gaia.yaml` contains `${VAR}` placeholders resolved from `.env` at load
time. Unresolved names are reported by `/health` and `/api/meta` rather than
failing opaquely.

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

### A wide table times out (`read operation timed out`)

Thinking mode is slow — a single 12-column critique has taken 495 s, and an
18-column table exceeded the 600 s timeout. Workaround today:

```bash
make reason ARGS="--table dbo.transactions --no-thinking"
```

This is a known limitation; see §10.

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
| **No profiling yet (Phase 3)** | `evidence_coverage` cannot earn its `profile` share, so nothing reaches `accepted` |
| **Wide bundles + thinking mode** | 18-column tables exceed the 600 s timeout; use `--no-thinking` |
| **`thinking` is not in the skip key** | Toggling it does not upgrade existing answers without `--force` |
| **No question answering yet** | Phases 4–5 build SQL generation and the chat UI |
| **No SME review UI yet** | Low-confidence items are stored and flagged, but reviewed by hand in SQL |
| **SQL Server only** | The dialect seam exists; other engines are a Phase 7 task |

### What "good" looks like today

On `nge_small`, from zero comments:

* `line_tax` → "Line Tax Amount"
* `quantity` → "Quantity Sold"
* `unit_price` → "Unit Price"
* `condition` → "Item Condition"

Correct, useful, and correctly *not* trusted — each scores in the
`deep_profile` band, because the catalog alone cannot prove them. That is the
system working: it knows what it does not know, and says so with a number.

