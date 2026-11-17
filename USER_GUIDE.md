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
| **Query** (Phase 4A) | `make query`, `make query-check` | One SQL statement validated by the SQL guard and run read-only, recorded in `chat.query_run` — the path every generated query will take |
| **Retrieve** (Phase 4B) | `make retrieve`, `make retrieval-eval` | The schema context for a business question — objects, meanings and their certainty, values, allowed joins — and its recall over a question set |
| **Golden** (Phase 4D) | `make golden`, `make clarifications` | A set of questions with known answers, run and scored by result; the clarifications Gaia asked and how often its own choice was the user's |
| **Chat API** (Phase 4E) | `make dev-api` | Conversations over HTTP: questions answered as server-sent events, clarifications and follow-ups as the next message |
| **Data sources** (Phase 4F) | `make datasource` | Databases added at runtime, password encrypted, usable only once proven read-only; then `TARGET=<name>` everywhere |
| **Ask** (Phase 4C) | `make ask` | A business question answered: a table and a narrative, or a clarifying question — with what was computed, the assumptions, the unconfirmed meanings relied on, and a confidence |
| **Sign-in and access** (Phase 5A) | `make admin-user`, `make auth`, `make dev-web` | People sign in with their company directory account; an administrator grants Gaia roles (user, SME, administrator, auditor) and the PII-processor permission; every decision audited; the first web UI (administration, audit) |

Targets: SQL Server (`demo_mssql`, still the config default) and PostgreSQL
(`demo_pg`, a synthetic development target). Every stage works on both; choose
with `TARGET=<name>` in `make`, or `--target <name>` on the tools. Since
2026-11-16 PostgreSQL is the **main dialect** (D52): every dialect change is
built and tested there first; SQL Server follows, and its live results no longer
block a delivery.

Built but still being run: the Phase 3 evaluation (3C, `make evaluate*`). Built
in Phase 4 so far: the SQL guard and the read-only executor (4A), retrieval (4B),
answering (4C), golden runs (4D), the chat API (4E), data sources added at runtime
(4F) — Phase 4 is complete (golden run on `demo_pg`: 100 % executable, 97 % correct). Phase 5
so far: sign-in through the company directory, roles, administration and audit, and the web
UI's first screens (5A). Not built yet: the review screens (5B), need-to-know (5C), the chat
screen (5D).

Since Phase 4C **Gaia answers questions** from the command line (`make ask`). Read the
confidence and the "unconfirmed" lines of every answer: the confidence is a heuristic until
Phase 6 calibrates it.

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

### Running SQL through the guard (`make query`, Phase 4A)

```bash
make query       TARGET=demo_pg ARGS="--sql 'SELECT count(*) FROM sales.orders'"
make query       TARGET=demo_pg ARGS="--file infra/pg-target/queries/01_revenue_by_city.sql"
make query-check TARGET=demo_pg ARGS="--file my.sql"        # validate only: no execution, no record
```

The guard checks the statement against Gaia's stored catalog snapshot and join
paths — not the live database — and prints what it found: `ACCEPTED` or
`REJECTED` with one line per violation (a code and a reason), the objects read,
every join and what backs it (`fk`, `hint`, `sme` — with `_via_view` when a view
column stands for its base column —, `shared_key` for two children of one key of
which one side is aggregated, `same_column`, `generated`; `NO unverified` or
`NO fan_out` otherwise — see Troubleshooting), the row cap, and the SQL
that will run. That SQL is rendered by the guard: schema-qualified, capped, and
exactly what was validated. Then it runs in a read-only session with the
target's statement timeout and prints the rows.

| Option | Meaning |
|---|---|
| `--sql TEXT` / `--file PATH` | the statement (a file path is relative to the repository root) |
| `--dry-run` | validate only (`make query-check`) |
| `--no-record` | do not write `chat.query_run` |
| `--question TEXT` | the business question, stored with the run |
| `--rows N` | rows to print (default 20; the cap is `query.row_limit`) |
| `--json` | machine-readable output |

Exit codes: 0 ok, 2 rejected, 3 the execution failed or timed out. Examples that
show what is accepted and refused: `infra/pg-target/queries/`,
`infra/sqlserver/queries/`.

What is refused, in short: anything but one `SELECT`/`WITH` query; writes and
locks anywhere inside it; objects not in the snapshot (including every system
catalog); functions not on the dialect's allowlist (e.g. `set_config`,
`pg_sleep`, `dblink`, `xp_cmdshell`, `OPENROWSET`); user functions; variables;
on SQL Server also other databases, linked servers, temp tables, locking hints
and `OPTION`; and joins with nothing behind them — comma or `CROSS` joins,
`NATURAL` joins, `LATERAL`/`APPLY`, or a condition between columns no FK, tested
hint, SME hint or view lineage connects.

### Asking a question (`make ask`, Phase 4C)

```bash
make ask TARGET=demo_pg ARGS="--question 'Which store city had the highest order amount last month?'"
make ask TARGET=demo_pg ARGS="--question 'Who are our best customers?' \
    --clarified 'the ten customers with the highest order amount in 2025'"
make ask TARGET=demo_pg ARGS="--question '…' --show-sql --as-of 2026-01-15"
```

What happens: retrieval builds the context (4B); the model either writes one query or
asks back; the SQL guard checks the query (4A); it runs read-only; if the guard refuses
it, the database rejects it or it runs too long, the model is told why and tries again
(`query.max_repair_attempts`, 2); finally the model writes a short narrative of the
result. Thinking is off for all of these calls (`llm.thinking.sql_generation`, `repair`,
`answer`).

An answer prints the narrative, the result table (`--rows N` rows; the table holds up to
`query.row_limit`), and:

| Line | Meaning |
|---|---|
| `confidence : medium (0.68)` and reasons | How sure Gaia is, and why: unconfirmed meanings relied on, repairs needed, joins resting on a model's hint, a result cut at the row limit, assumptions. A heuristic until Phase 6 calibrates it |
| `computed :` | What the query computed, in business words, with the period and the population |
| `assumption :` | A choice the question left open and the model made |
| `unconfirmed :` | A meaning the answer relies on that is machine-inferred and not accepted (or that does not exist yet) |

When the question can be read in more than one way, Gaia does not guess (P4-4): it prints
`clarification :` with numbered options and the command to answer it:

```bash
make ask TARGET=demo_pg ARGS="--clarification <id> --clarified 2"              # an option
make ask TARGET=demo_pg ARGS="--clarification <id> --clarified 'only 2025, by units'"
```

Every clarification is stored (`chat.clarification`, D61) with Gaia's best guess — the
option it would have chosen and that option's query, checked by the guard but not run.
Which option that is, is not shown to you on purpose: the point is to measure how often
Gaia's choice is yours (`make clarifications`), and a marked option would pull answers
towards it. When you choose Gaia's option, the stored query answers at once, without
another model call. When the data is not there, it prints `cannot answer :` and why. Exit
codes: 0 answered, 4 clarification, 5 cannot answer, 3 failed.

Relative periods ("last month") are computed from today's date, or from `--as-of` /
`query.as_of` — use that when the data ends in the past, as on `demo_pg` (2025).

The SQL is not shown (P4-2) unless `--show-sql`, which prints every attempt and what went
wrong with it. Every attempt is stored in `chat.query_run` (chained by `parent_id`) and
every model call in `audit.llm_call`; `--no-record` skips both. A model remote from your
data (`llm.locality: remote`) never sees the values of personal-data columns in a result:
they reach it as `[hidden]`. `--replay DIR` answers from files (tests; recorded as model
`replay`).

### The chat API (`make dev-api`, Phase 4E)

`make dev-api` serves the API on `127.0.0.1:8000` (keep it local until it is behind TLS, P5-9).
Since Phase 5A every endpoint below needs a session with a chat role (`auth.chat_roles`:
administrators and SMEs until need-to-know, 5C) — see "Sign-in and access" below — and a
conversation belongs to whoever started it.

| Endpoint | Does |
|---|---|
| `GET /api/data-sources` | the data sources — from `config/gaia.yaml` and added at runtime (4F): name, dialect, origin, status, introspected, usable, default |
| `POST /api/conversations` `{"data_source": "demo_pg", "title"?}` | a new conversation (201) |
| `GET /api/conversations?data_source=demo_pg` | conversations, newest first |
| `GET /api/conversations/{id}` | a conversation and its turns |
| `POST /api/conversations/{id}/messages` `{"content", "as_of"?, "reply_to_clarification"?}` | a turn: server-sent events, or JSON with `?stream=false` |

A turn streams: `message` (your turn, stored) → `status` for each stage (retrieval,
retrieved, generation, checking, running, repairing, narrating) → `result` (the table, as
soon as it exists) → one of `answer`, `clarification`, `cannot_answer`, `failed` (Gaia's turn,
stored) → `done`. Lines starting with `:` are keep-alives.

```bash
curl -s -c jar -H 'content-type: application/json' \
     -d '{"login": "akaya", "password": "…"}' 127.0.0.1:8000/api/auth/login     # a session
CSRF=$(awk '/gaia_csrf/ {print $7}' jar)
curl -sN -b jar -H "X-CSRF-Token: $CSRF" -X POST 127.0.0.1:8000/api/conversations/$CID/messages \
     -H 'content-type: application/json' -d '{"content": "Top 5 shops performing best last year."}'
```

When Gaia's last turn is a clarification, your next message answers it — `1` for the first
option, or your own words; send `"reply_to_clarification": false` to ask something new
instead. A follow-up ("and in 2024?", "only Ankara") is read with the last
`chat.history_turns` answered turns. The `answer` event carries the narrative, the table
(every row, up to `query.row_limit`), what was computed, assumptions, unconfirmed meanings
and the confidence — never the SQL (P4-2). The stored turn keeps `chat.keep_rows` rows, with
personal-data columns masked.

### Data sources added at runtime (`make datasource`, Phase 4F)

Targets in `config/gaia.yaml` work as before. Any other database can be added without
editing a file; its password is stored encrypted in Gaia's metadata store.

```bash
make master-key >> .env                    # once: GAIA_MASTER_KEY=…  (keep a copy; see below)
read -rs PW; export PW                     # the read-only login's password, not echoed
make datasource ARGS="add --name sales_pg --dialect postgres --host 10.0.0.5 --port 5432 --database sales --user gaia_ro --password-env PW"
make datasource ARGS="probe sales_pg"      # the read-only probe: 9 checks, as make check-target
make introspect TARGET=sales_pg            # from here on, every command takes TARGET=sales_pg
make datasource ARGS="list"                # config and added ones: origin, status, last probe
make datasource ARGS="update sales_pg --host 10.0.0.6"     # any change: probe again
make datasource ARGS="disable sales_pg"    # | enable | remove (asks; --yes)
```

Without `--password-env` the password is asked for. Options: `--option sslmode=require`
(repeatable), `--limit statement_timeout_s=20` (the limits of `targets.*`). The audit
record names you by `$USER`.

* **Unusable until proven read-only.** A new data source is `unverified`; any command
  with it says so and names the fix. `probe` runs the same nine checks as `make
  check-target` — privileges, and a write that must fail. Passed: `verified`; failed:
  `failed` with the failing checks (a login that can write always fails). Any change makes it
  `unverified` again, and so does a proof older than `datasources.reprobe_days` (30).
* **Removal** keeps the row and everything computed for it (snapshots, meanings, runs),
  wipes the password and frees the name: the old one is listed as `sales_pg~1a2b3c4d`.
* **Every action** is recorded in `audit.connection_change` — who, what, which fields;
  never a value.
* **The master key.** Lose it and the stored passwords are lost (add the data sources
  again); the rest of the store is unaffected. To change it: put the old key in
  `GAIA_MASTER_KEY_PREVIOUS`, a new one (`make master-key`) in `GAIA_MASTER_KEY`, run
  `make datasource ARGS="rotate-key"`, then remove the previous key.
* **Over HTTP and in the web UI** (Phase 5A), `/api/admin/data-sources` does the same for a
  signed-in administrator; the audit names the administrator's login.

### Sign-in and access (`make admin-user`, `make auth`, Phase 5A)

People sign in with their company directory account (Active Directory / LDAP over TLS; Entra
ID follows). Gaia never keeps their passwords: the directory checks them at every sign-in.
What someone may do in Gaia comes from **grants** an administrator makes:

| Gaia role / permission | Lets someone |
|---|---|
| `user` | ask questions (the chat; open to users once need-to-know exists, 5C) |
| `sme` (per data source) | review what Gaia understood about that data source (5B); ask |
| `admin` | connect data sources and directories, grant and revoke; ask. Never review |
| `auditor` | read the audit reports. Never an administrator too |
| `pii_processor` | see personal data unmasked in answers (5C) |

**First time:** the one local account, `gaia-admin`, is for setup and emergencies.

```bash
make admin-user                          # its password, asked twice (≥ 12 characters)
make admin-user ARGS=setup-code          # or: a one-time code to set it on the sign-in screen
make dev-api                             # and in a second terminal: make dev-web
                                         # then http://127.0.0.1:5173
```

**A directory** (the admin screen "Directories", or the command line). Write its settings in a
JSON file like `infra/auth/acme-samba.json`: the server URLs (`ldaps://…`, or `ldap://…` with
`"start_tls": true` — never clear text), the service account's DN (it only reads), where
people are, and **how roles are read**: group membership (`"kind": "group"`, nesting
followed), a user attribute (`"kind": "attribute", "attribute": "department"`) or the OU
(`"kind": "ou"`).

```bash
make auth ARGS="directory add acme --settings ../infra/auth/acme.json"   # the password is asked
make auth ARGS="directory probe acme"        # test the connection: transport, bind, users, roles
make auth ARGS="directory sync acme"         # read every user and role now
make auth ARGS="directory roles acme"        # the distinct roles, with members
make auth ARGS="grant admin --role acme/GAIA-Admins"
make auth ARGS="grant user --role acme/GAIA-Finance"
make auth ARGS="grant sme --user acme/akaya --data-source sales_pg"
make auth ARGS="grant auditor --role acme/Internal-Audit"
make auth ARGS="grants"                      # --all: revoked ones too
make auth ARGS="revoke <grant id> --note 'moved team'"
make auth ARGS="accounts"                    # who may do what now, conflicts included
make auth ARGS="audit-check"                 # is the access audit intact; its head hash
```

* **No role, no entry.** A person without any grant — directly or through a directory role —
  cannot sign in, and hears only "sign-in failed", as for a wrong password; the audit keeps the
  real reason.
* **Sessions** end after 30 minutes without activity or 8 hours at most. Every 15 minutes the
  directory is asked again, so someone disabled there, or left without a Gaia role, is signed
  out within that time.
* **Five wrong passwords** lock an account for 15 minutes in Gaia (before the directory's
  own lockout counts them).
* **Everything is audited** in `audit.auth_event`: append-only and hash-chained, so an edit in
  the database shows (`make auth ARGS=audit-check`). The auditor sees it in the web UI
  (Audit), with CSV downloads.
* **From a script**, sign in with `POST /api/auth/login` and send the `X-CSRF-Token` header
  (the value of the `gaia_csrf` cookie) with every request that changes something.

### Golden runs (`make golden`, Phase 4D)

```bash
make golden TARGET=demo_pg ARGS="--file infra/pg-target/golden.yaml --out evaluations/golden_demo_pg"
make golden TARGET=demo_pg ARGS="… --only g01,o03"
make clarifications TARGET=demo_pg        # users' clarifications; --all adds golden and test runs
```

A golden file lists questions with known answers (`infra/pg-target/golden.yaml` documents
the format). `expect: answer` questions have a reference query; `expect: clarify` questions
must be asked back, and may carry the user's answer (`clarified`) and the reference for it.
`identify_by` lists columns that identify the same thing (sku, name, id: one is enough);
`rank_by` the column a top-N list is ranked by (rows tied at the cut are interchangeable);
`order_matters` that the order is part of the answer. `as_of` is "today" for the whole set.

The run asks every question as a user would, clarifies where the file says so, and scores
the final answer by comparing its *result* with the reference's: columns by their values
(names and extra columns do not matter), numbers at the precision shown, rows as a set or in
order. It prints one line per question and writes `report.md` and `results.json` to `--out`:
executable and correct shares (the Phase 4 targets: 80 % and 70 %), how often Gaia asked back
when it should and when it should not, how often its best guess was the user's answer, and
the cost. Every model answer is kept in `--out/llm`: an interrupted run resumes and a repeated
run makes no new call.

### Finding what a question needs (`make retrieve`, Phase 4B)

```bash
make retrieve TARGET=demo_pg ARGS="--question 'revenue by store city last month'"
make retrieve TARGET=demo_pg ARGS="--question '…' --context"        # + the text the generator sees
make retrieval-eval TARGET=demo_pg ARGS="--file infra/pg-target/retrieval_questions.csv"
```

`make retrieve` prints the ranking (the ten best objects, `*` = in the context), the
context (each object with why it was chosen: `lexical`, `value`, `vector`, or `bridge`
on a join path), the joins offered, how many meanings in it are uncertain, notes, and
the size in tokens; `--context` adds the rendered context itself, `--json` everything
as JSON, `--no-vectors` ranks by words only. It reads Gaia's store and asks Ollama for
the question's embedding — if Ollama is down it says so and ranks by words.

In the context a column reads like
`total_amount numeric(14,2) — Order Total [measure, TRY] (uncertain 0.62)`: its type,
its meaning, role and unit, and `(uncertain …)` when the meaning is machine-inferred
and not accepted, `(no meaning yet)` when the object was never reasoned. Readable
values (`values: 'web', 'store', …`, or `most frequent values (10 of ~40)`), date
ranges and conversion hints (`integer date yyyymmdd: to_date(…)`) follow. Personal
data never shows values. Only joins the SQL guard accepts are listed.

`make retrieval-eval` reads a CSV `question,expected[,notes]` — `expected` lists the
objects a correct answer needs (`schema.object`, separated by `|`; alternative sets by
`;`) — and prints per question `ok` (all expected objects in the context and joined),
`part` (in the context, not joined) or `MISS`, then `covered`, `connected`, `recall@k`,
`MRR` and the mean context size. `--out F` writes everything as JSON.

Settings: `retrieval.*` in `config/gaia.yaml` (objects per context, bridges, join hops,
columns shown, token budget, nearest meanings from the embeddings).

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
| `llm.concurrency` | 3 | **Never raise above 3.** The gateway caps at 4 per model; a retry needs a free slot. Per command: `GAIA_LLM_CONCURRENCY=2 make …` — two jobs at once at 2 each share the 4 slots |
| `datasources.reprobe_days` / `admin_api` | 30 / false | How long a data source added at runtime stays usable after its probe; the admin HTTP API (off until logins exist) |
| `llm.timeout_s` | 1800 | Read timeout per completion. Sized for the widest bundle plus thinking-mode headroom |
| `llm.thinking.semantics` / `.critique` | true | Turn off to trade depth for speed (~4–5× faster) |
| `llm.retry.max_attempts_rate_limit` | 8 | Raise only if the gateway gets flakier |
| `llm.max_output_tokens` | 16000 | Usually leave it. It covers the model's thinking *and* the answer; a truncated answer is re-asked in smaller column chunks automatically. Raising it lengthens the worst call (at ~15 tokens/s, 32k tokens outlast `llm.timeout_s`) and changes every request key, so cached and recorded answers stop matching |
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

Only `.env`, never `config/gaia.yaml`, never the database in clear. `.env` is
git-ignored. The one exception by design: passwords of data sources added at runtime
(4F) are in the metadata store, **encrypted** with `GAIA_MASTER_KEY` from `.env`. If a key is missing, `make reason` fails with a clear message;
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

### `output truncated at max_tokens=16000: thinking T, answer A token(s)`

With thinking on, `llm.max_output_tokens` is shared by the chain of thought and
the answer, so even a narrow table can run out when the model thinks long (3C,
2026-11-16: 12,437 of 16,000 tokens were thinking on a 14-column table). Since
2026-11-16 (D53) the reasoner handles it: it asks again with at most half the
columns of the chunk that truncated (14 → 7 → 4 → 2), and a truncated critique
is asked again in halves. The run prints `answer truncated on N column(s):
asking again …`. Only if even 2-column chunks truncate does the table fail.

Do **not** raise `llm.max_output_tokens` to cure this: the calls get longer than
the read timeout allows at this gateway's speed (median ~15 tokens/s, slowest
~8), and every request key changes, so cached answers and an evaluation run's
recorded answers no longer apply.

### `data source 'x' is unverified` / `failed` / `was last proven read-only more than … days ago`

A data source added at runtime (4F) is used only while its read-only probe is current.
`make datasource ARGS="probe x"`; if it ends `failed`, the listed checks say why — usually
a login with more than read rights (`security.privileges`). `GAIA_MASTER_KEY is not set` or
`does not decrypt`: the key in `.env` is missing or not the one the password was stored with
(§5, "Data sources added at runtime").

### `make query`: `REJECTED … unverified_join`

The join's condition connects two columns Gaia has no join path for. Either the
join is wrong, or the relationship exists but nothing records it yet: declare the
FK in the database, let the model propose it (`make reason`) and Stage 3 test it
(`make profile-tests`) — a model hint counts once its test passed and was
specific (D48) — or, from Phase 5, confirm it as an SME. Two tables that both
reference the same key (two facts on `customer_id`) join as `shared_key` once one
side is aggregated by that key (otherwise: the fan trap, below).
`query.unverified_joins: warn` turns this rule — and the fan-trap rule — into
warnings; it is meant for diagnosis, not for answering questions.

### `make query`: `REJECTED … neither side of this join is unique`

The join follows a join path, but both sides can repeat, so every row on one side
meets every matching row on the other — `order_lines JOIN payments ON order_id` pairs
each payment with each line of its order, and any sum over it is inflated (a *fan
trap*). Aggregate one side by the join column first (`WITH p AS (SELECT order_id,
sum(amount) … GROUP BY order_id)`), or join each to the parent (`orders`) separately.

### `make query`: `REJECTED … function_not_allowed`

Functions are on a per-dialect allowlist (`dialects/<d>/guard.py`). A missing
harmless function is added there with a test; a function that changes settings,
reads files, sleeps, locks, writes or runs other SQL is never added.

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

