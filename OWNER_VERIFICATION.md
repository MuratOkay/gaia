# Owner verification — what to run on your machine

Everything in Phase 3 so far (3.P, 3.0, 3B, 3A) was built and tested in a
sandbox that has PostgreSQL 16 but **no SQL Server, no Docker, no Ollama and no
LLM gateway**.
This file lists, in order, what only your machine can verify, the exact
commands, what you should see, and what to send back.

Work through it top to bottom; each section depends on the previous one.
Tick the boxes as you go. Times are rough.

| § | What | Needs | Time |
|---|---|---|---|
| 1 | Update the environment and migrate the metadata store | `.env`, metadata DB | 5 min |
| 2 | Unit tests (no database) | nothing | 1 min |
| 3 | PostgreSQL development target on your host (3.P) | Docker | 10 min |
| 4 | SQL Server checks (3.0): timeout, read-only probe | `sql1` | 5 min |
| 5 | Cold-cache measurement on SQL Server (3.0) — **before anything else reads SQL Server data** | `sql1`, `sa` to restart | ≤ 90 min, unattended |
| 6 | Phase 3B with your configured model | LLM gateway, `demo_pg` | 20–40 min |
| 7 | Phase 3A on `demo_pg`: profile, reason, test, re-reason | LLM gateway | 1–2 h (mostly LLM) |
| 8 | Phase 3A on SQL Server: profile, footprint, reason | `sql1`, LLM gateway | 1–3 h (mostly LLM) |
| 9 | The full test suite with every live test | all of the above | 5 min |
| 10 | What to send back | — | — |
| 11 | After the 2026-11-15 fixes and decisions: migrate, test, profile, test claims, re-score, reason | `sql1`, `demo_pg`, LLM gateway (11e only) | 30 min + LLM |
| 12 | After the §11 review: test, re-profile SQL Server, re-score, queue (no LLM) | `sql1`, `demo_pg` | 15 min |
| 13 | Phase 3C: labels, evaluation runs, judge, audit, report, production cost | LLM gateway | 1 h + overnight |

Why §5 comes before §8: profiling makes SQL Server create statistics on the
columns it reads, and the measurement in §5 is the one that records that
footprint from a clean state. If §8 ran first, §5 would see fewer new statistics.

**Secrets.** The repository contains no API keys or passwords; `.env.example`
holds placeholders only. Your real values stay in your `.env`.

---

## 1 · Environment and migration

New since your last run: the PostgreSQL target variables (3.P), migration
`0004` (3B: evidence ledger and review queue) and migration `0005` (3A: the
`profiling` schema). `config/gaia.yaml` now has `profiling.enabled: true` and a
`profiling.budget` block.

```bash
cd /home/murat/g120s/gaia
diff <(grep -o '^[A-Z_]*=' .env | sort) <(grep -o '^[A-Z_]*=' .env.example | sort)
```

Copy any variable that exists only in `.env.example` into `.env`. At least:

```bash
GAIA_PG_TARGET_PASSWORD=<choose one>            # read-only role of the PG target
GAIA_PG_TARGET_ADMIN_PASSWORD=<choose one>      # owner of the PG target container
GAIA_PG_TARGET_ADMIN_URL=postgresql://postgres:<admin pw>@127.0.0.1:5434/gaia_target_pg
```

Quote a value in `.env` if it contains `;`, `#` or spaces. In **URLs**,
percent-encode special characters (`;` → `%3B`, `@` → `%40`, `:` → `%3A`).

```bash
make venv                                        # no new dependencies, refreshes the editable install
make up
cd backend && ../gaia-env/bin/alembic upgrade head && cd ..
```

- [x] `alembic upgrade head` ends with
      `Running upgrade 0004 -> 0005, Profiling (Phase 3A): runs, table/column profiles, tests, modules, view lineage`
      (preceded by `0003 -> 0004` if 3B's migration was not applied yet; or reports
      `0005 (head)` if already applied)

If you ever need to undo it: `cd backend && ../gaia-env/bin/alembic downgrade 0004`
(drops the `profiling` tables) or `downgrade 0003` (also 3B's).

---

## 2 · Unit tests (no database needed)

```bash
make test
```

==> The output is:
335 passed, 30 skipped in 13.33s

- [x] **335 passed, 30 skipped** if Ollama is running (`333 passed, 32 skipped` if it is not).
      The skips are the live tests enabled in §9.

If one fails, stop and send the output (§10).

---

## 3 · PostgreSQL development target on your host (3.P)

Verified in the sandbox on PostgreSQL 16; your Compose service uses `postgres:18`.

```bash
make pg-target-up                                   # starts gaia-target-pg on 127.0.0.1:5434
make pg-target-seed                                 # ~3 M rows, ~1.5 min; deterministic
make check-target TARGET=demo_pg
make introspect   TARGET=demo_pg
make introspect   TARGET=demo_pg                    # second time
make reason-dry   TARGET=demo_pg ARGS="--table legacy.tb_trx02 --show-bundles"
```

- [x] `pg-target-seed` ends with `loaded 2,973,244 rows into gaia_target_pg (scale=1.0, seed=42)`
- [x] `check-target`: **9 × PASS**, the last one `write denied as expected in schema public (SQLSTATE 42501)`
- [x] first `introspect`: `28 (24 tables, 4 views)`, `columns : 213`
- [x] second `introspect`: `unchanged=28`, `tables written: 0`, `columns written: 0`
- [x] `reason-dry` prints a bundle whose columns are `f1` … `f10`, followed by an
      `Evidence (json):` block saying `catalog only` (nothing is profiled yet —
      expected; §7 profiles it)

PostgreSQL 18 specifics the sandbox could not test: NOT NULL constraints listed
in `pg_constraint`, virtual generated columns. If `introspect` fails or the
counts differ, send the output.

---

## 4 · SQL Server checks (3.0)

Nothing here writes to SQL Server: the probe's `CREATE TABLE` is denied and rolled back.

```bash
make check-target
```
==> The output is:
[PASS] connect              connected to 127.0.0.1:5434
[PASS] server.version       PostgreSQL 18.7 (Debian 18.7-1.pgdg13+2)
[PASS] server.context       database=gaia_target_pg role=gaia_ro
[PASS] catalog.read         tables=24 views=4 columns=213
[PASS] catalog.comments     table comments=1 column comments=2
[PASS] catalog.stats        pg_stat_user_tables rows=29
[PASS] catalog.select       SELECT granted on every table and view
[PASS] security.privileges  owns nothing; no write, CREATE or bypassing role attribute
[PASS] security.read_only   write denied as expected in schema public (SQLSTATE 42501)


- [x] **9 checks** (was 7), all PASS. New: `catalog.select` and `security.privileges`.
- [x] `security.read_only` says `write denied as expected in schema dbo (error …)` with a
      *permission* error number (262 expected). Before 3.0 any error counted.

If `security.privileges` **fails**, the line names the permission, e.g.
`database permissions: EXECUTE` or `member of db_datawriter`. Either it is a
real write path (remove it), or it is a harmless permission the allow-list does
not know yet — send me the exact line and I will add it.

The live SQL Server tests (statement timeout really cancels, probe, deterministic
catalog read, the profiler) run in §9 with `GAIA_TEST_MSSQL=1`.

---

## 5 · Cold-cache measurement on SQL Server (3.0)

Unattended, read-only (`SELECT`/`SET` only), at most 90 minutes. The exact
distinct counts make SQL Server **auto-create statistics** on the measured
columns — that footprint is one of the things being measured, and the report
lists every statistic created.

```bash
docker restart sql1
# wait until it accepts connections (the connect check passes):
until make check-target 2>&1 | grep -q 'PASS\] connect'; do sleep 5; done
make measure TARGET=demo_mssql ARGS="--table dw.sales_wide --table dw.fact_sales --table dbo.transaction_lines"
```

The report is printed and saved; the last line gives both paths, e.g.
`/tmp/gaia-measure-demo_mssql-2026-11-15T101500.json` and `.md`.

==> The output files can be found at:
verification/gaia-measure-demo_mssql-2026-11-14T215622.json and 
verification/gaia-measure-demo_mssql-2026-11-14T215622.md

- [x] `Findings` includes `first COUNT physical reads = N (cold cache)` with N > 0
      for at least the first table — proof that the run started cold
- [x] each table lists `exact tier 1+2 ... = X s` (the number 3A's budget needs)
- [x] `footprint:` line and a `Statistics created during the run` table
- [x] `TABLESAMPLE on ... columnstore ...: supported` (or `NOT supported — <error>`)

If the `Pages` and `Physical` columns are empty, your ODBC driver did not return
the `STATISTICS IO` messages to pyodbc (this could not be tested without SQL
Server). The timings are still valid — tell me, and I will capture IO another way.

Optional, for a like-for-like comparison with PostgreSQL:
`make measure TARGET=demo_pg` (seconds at scale 1).

---

## 6 · Phase 3B with your configured model

3B was built before 3A (profiling) at your request, against a JSON evidence
file. The steps below use the same file as my sandbox run (`--evidence-file`
overrides the profile store), so your results are comparable with mine. §7 then
does the same with evidence your own profiling run collected.

### 6a · Replay reproduction (no LLM calls, deterministic)

This replays the six answers I wrote as the model in the sandbox. **They are not
accuracy results**: I designed `demo_pg` and knew every answer; the point is to
show that your installation reproduces the pipeline exactly. See
`tests/fixtures/replay_3b/README.md`.

```bash
rm -rf /tmp/replay_3b && cp -r tests/fixtures/replay_3b /tmp/replay_3b
make reason TARGET=demo_pg ARGS="--replay /tmp/replay_3b --evidence-file /tmp/replay_3b/evidence.json \
  --table legacy.tb_trx02 --table legacy.tb_mst01 --table hr.departments --no-embed"
make review TARGET=demo_pg
```

==> The output files can be found at:
verification/6a_make_reason.out and 
verification/6a_make_review.out

- [x] `model : replay`, `generation : 2 (evidence, heuristic-v2); values exposed as masked`
- [x] `reasoned=3 failed=0`, `llm : calls=6`, no `awaiting an answer` lines
- [x] `bands : {'accepted': 10, 'deep_profile': 8, 'deep_profile_flagged': 2, 'abstain': 1}`
- [x] `make review` lists **11** items; the first two are `legacy.tb_mst01.c07` (a "credit
      limit" stored as text — abstain) and `legacy.tb_trx02.f2` (a reference with no
      declared FK and no inclusion test yet)

This needs the **default** seed (`--scale 1 --seed 42`, §3): the bundle contains
the row estimate, so another scale gives other prompts. If some requests are
reported as *awaiting an answer*, your prompts differ from the sandbox's (most
likely PostgreSQL 18 renders a type or a default differently in the snapshot, or
`ANALYZE` estimated a row count slightly differently). Send the `diff` of one request file against the sandbox's
(`/tmp/replay_3b/requests/<key>.md`); the pipeline itself is still fine.

### 6b · The same three tables with your configured model

Now the real thing. The replay run stored its versions as model `replay`; the
model is part of the skip key, so these tables are re-reasoned with DeepSeek.
About 6 LLM calls (more if a table is chunked or a validation retry happens).

```bash
make reason-dry TARGET=demo_pg ARGS="--evidence-file /tmp/replay_3b/evidence.json \
  --table legacy.tb_mst01 --show-bundles" | grep -E 'AAAA|9\{12\}' | head -3
make reason TARGET=demo_pg ARGS="--evidence-file /tmp/replay_3b/evidence.json \
  --table legacy.tb_trx02 --table legacy.tb_mst01 --table hr.departments --cost-report"
make review TARGET=demo_pg
```

==> The output files can be found at:
verification/6b_reason_dry.out  
verification/6a_reason.out
verification/6a_review.out

- [x] the dry run shows person names and phone numbers only as shapes
      (`AAAAA AAAA`, `9{12}`) — masked exposure for a remote model
- [x] `reasoned=3 failed=0`; every call in `--cost-report` ends with `yes`
      (validated); note how many calls needed a second attempt (`att 2`) --> 0 calls see out file.
- [x] the review queue is printed

Things worth looking at, and worth sending:

* Did DeepSeek use the evidence? In `make review` and in the SQL below, look at
  `legacy.tb_trx02`: with the view aliases and the identity `f7 = f5 × f6`, most
  of its columns should be accepted, as in the replay.
* `semantic_type` claims vs detectors:

```sql
-- make psql
SELECT k.column_name, c.business_name, c.role, c.claimed_semantic_type,
       c.semantic_type, round(c.raw_score::numeric, 3) AS score, c.band, c.coverage
FROM semantics.column_semantic c
JOIN catalog.columns k ON k.id = c.column_id
JOIN catalog.tables  t ON t.id = c.table_id
WHERE c.is_current AND t.object_name IN ('tb_trx02', 'tb_mst01', 'departments')
ORDER BY t.object_name, k.ordinal;
```

==> See the output at verification/6b_sqlresult.out
---

## 7 · Phase 3A on `demo_pg`

Profiling reads the target (`SELECT` only, read-only session, budgets, one run
at a time) and stores aggregates, never rows. Nothing here needs the LLM until
`make reason`.

```bash
make profile-dry TARGET=demo_pg
make profile     TARGET=demo_pg ARGS="--report"
make profile     TARGET=demo_pg                      # second time: reuse
```

==> The outputs are at:
verification/7_profile_dry.out
verification/7_profile_1.out
verification/7_profile_2.out



- [x] `profile-dry`: `{'skipped': 4, 'would_profile': 24}` (the 4 are views)
- [x] first `profile`: `tables : {'profiled': 24, 'skipped': 4}`; `modules : {'new': 4, …}`;
      `view lineage : {'pass_through': 16, 'computed': 12, 'unresolved': 0}`;
      `footprint : … new: 0` (PostgreSQL never creates statistics by itself).
      Sandbox (PostgreSQL 16): 169 data queries, ~25 s, slowest table ~4 s
- [x] `join candidates : tested=17 kept=8 passed(>=95% of rows)=6`, including
      `legacy.tb_trx02.f2 → crm.customers.customer_code 99% rows` and
      `legacy.tb_trx02.f9 → store.stores.store_code 98% rows  33% values`
      (two thirds of `f9`'s codes are orphans, but they are 2 % of the rows)
- [x] second `profile`: `{'reused': 24, 'skipped': 4}` and **`data queries=0`**

Then reason with these profiles — the legacy tables and the reporting views
(~8 objects, ~16–20 calls):

```bash
make reason-dry TARGET=demo_pg ARGS="--table legacy.tb_trx02 --show-bundles" | grep -A4 '"aliases"' | head -40
make reason     TARGET=demo_pg ARGS="--schema legacy --schema rpt --cost-report"
make profile-tests TARGET=demo_pg ARGS="--schema legacy --schema rpt --dry-run"
make profile-tests TARGET=demo_pg ARGS="--schema legacy --schema rpt"
make reason     TARGET=demo_pg ARGS="--schema legacy --schema rpt"
make review     TARGET=demo_pg
```
==> The outputs are at:
verification/7_reason_dry.out and verification/7_reason_dry_full.out
verification/7_reason.out
verification/7_profile_tests_dry.out
verification/7_profile_tests.out
verification/7_reason_nocost.out
verification/7_review.out
verification/7_sqlresult.out (for the sql)

- [x] the dry run shows aliases such as `transaction_id` for `f1` and
      `transaction_date` for `f3` with `"via": "TO_DATE(…)"`
- [x] first `reason`: every object `reasoned`, `failed=0`
- [x] `profile-tests` lists the tests it derived from DeepSeek's claims (join hints →
      `inc:…`, identifiers with estimated counts → `uniq:…`, codes/flags → `topk:…`),
      possibly none — that is a valid outcome; send the list either way
- [x] second `reason`: only tables whose tests produced new evidence are `reasoned`
      (`evidence changed`); the rest `skipped`
- [x] `make review` prints the queue

```sql
-- make psql: what DeepSeek concluded for the worst-case tables, and how sure the ledger is
SELECT t.object_name, k.column_name, c.business_name, c.role, c.claimed_semantic_type,
       c.semantic_type, round(c.raw_score::numeric, 3) AS score, c.band, c.coverage
FROM semantics.column_semantic c
JOIN catalog.columns k ON k.id = c.column_id
JOIN catalog.tables  t ON t.id = c.table_id
WHERE c.is_current AND t.schema_name IN ('legacy', 'rpt')
ORDER BY t.object_name, k.ordinal;
```

This is the first run in which a real model sees evidence Gaia collected itself.
It is still not an accuracy measurement (that is 3C), but it is the first honest
look at what the worst-case tables get from it.

---

## 8 · Phase 3A on SQL Server

Run this **after §5**. Profiling is read-only, but SQL Server creates statistics
for columns it is asked to count or group and has none for; the run lists every
one (the footprint, D4′). Start with the Phase 2 scope:

```bash
make profile-dry ARGS="--table dbo.transactions --with-neighbours"
make profile     ARGS="--table dbo.transactions --with-neighbours --report"
make profile     ARGS="--table dbo.transactions --with-neighbours"        # reuse
```

==> The outputs are at:
verification/8_profile_dry.out
verification/8_profile_report.out
verification/8_profile_reuse.out


- [x] every table ends `profiled`, `partial` or `skipped (reason)`; nothing `failed`
- [x] no table over ~180 s (the per-table ceiling; a statement is never started if
      it could run past it)
- [x] `footprint : statistics N -> M, new: K` with the list of new statistics —
      **send it**; it is the number to show a DBA
- [x] any `partial` table: its notes say what degraded and why (e.g. a
      `TABLESAMPLE` SQL Server refused on a columnstore table) — **send them**
- [x] second run: every table `reused`, `data queries=0`

Then the v2 reasoning of these tables (they were reasoned with Phase 2 prompts,
so the first v2 run re-reasons them once: ~20 calls, Phase 2 took ~75 min for
these with thinking), the claim tests, and the queue:

```bash
make reason        ARGS="--table dbo.transactions --with-neighbours --cost-report"
make profile-tests ARGS="--table dbo.transactions --with-neighbours"
make reason        ARGS="--table dbo.transactions --with-neighbours"
make review
```

==> The outputs are at:
verification/8_reason_cost.out
verification/8_profile_tests.out
verification/8_reason.out
verification/8_review.out

- [x] `reasoned=10 failed=0` (first run); the second `reason` re-reasons only
      tables whose evidence changed
- [ ] columns with a key, FK, constraint, detector hit or passed test can reach
      `accepted`; names alone cannot (they cap at 0.65)

Optional, in a quiet hour: `make profile` without `ARGS` profiles all 71 objects.
Send the summary lines (tables, data queries, pages, wall time, footprint).
==> verification/optional_out


To stay on Phase 2 behaviour instead: `semantics.prompt_major: 1` and
`scoring.version: heuristic-v1` in `config/gaia.yaml`; to keep profiling away
from a target: `targets.<name>.profiling_enabled: false`.

---

## 9 · The full test suite, every live test enabled

```bash
make test-db                                        # creates gaia_test in the metadata container
export GAIA_TEST_DATABASE_URL='postgresql+asyncpg://gaia:<metadata pw>@127.0.0.1:5433/gaia_test'
export GAIA_TEST_PG_TARGET_URL='postgresql://gaia_ro:<url-encoded pg target pw>@127.0.0.1:5434/gaia_target_pg'
export GAIA_TEST_MSSQL=1
make test
```
==> verification/all_tests.out

- [ ] **365 passed, 0 skipped** (Ollama up). Without Ollama: 363 passed, 2 skipped.
- [ ] In particular these ran (use `make test ARGS="-rA -q"` to list them):
  - `test_mssql_target_live.py` — a 6 s `WAITFOR` is cancelled after ~2 s as
    `StatementTimeout` and the session keeps working; the probe passes 9/9 for
    `gaia_ro`; two catalog reads give the same structure hash; **the profiler**
    runs every statement it generates on the smallest table without an error,
    completes on the largest (columnstore if any) table, and reads module
    definitions into view lineage. Add `-s` to see what it prints for the large
    table (status, size class, time, the sample's metric) — worth sending
  - `test_semantics_pipeline_pg.py::test_single_column_change_reasons_exactly_one_bundle`
    — the Phase 2 criterion that never ran on your host before
  - `test_reasoning_v2_pg.py` — the 3B pipeline end to end (fake model)
  - `test_profiling_pg.py` — the 3A pipeline end to end on `demo_pg` (fake model)
  - `test_replay_fixture_pg.py` — the replay fixtures still reproduce (scale 1 only)
  - `test_pg_target_live.py` — the PostgreSQL dialect against your `postgres:18`

`GAIA_TEST_DATABASE_URL` must point at a database whose name contains `test`:
the tests drop and recreate its schemas. **Never** point it at `gaia`.

To run one file: `make test ARGS="tests/integration/test_mssql_target_live.py -v"`.

---

## 10 · What to send back

1. **§2 / §9**: the last line of each `make test` (counts), and the full output
   of anything that failed; for §9 also the `-s` output of
   `test_profiler_on_a_large_table_records_every_degradation`.
2. **§3**: anything that differed from the expected results.
3. **§4**: the `make check-target` output (9 lines).
4. **§5**: the measurement report, the `.md` file (the `.json` too if convenient).
5. **§6a**: the bands line and the first five lines of `make review`.
6. **§6b**: the `make reason` summary (with `--cost-report`), `make review`, and
   the SQL result for the three tables.
7. **§7**: both `make profile` outputs, the `profile-tests` output, the second
   `make reason` summary, `make review`, and the SQL result.
8. **§8**: both `make profile` outputs (with `--report`) — above all the
   **footprint** list and any `partial` table with its notes — the `make reason`
   summary and the first 20 lines of `make review`.

With these I record the SQL Server numbers, tune the 3A budget defaults from
both databases' data (first `full_scan_max_pages`), and ask for the 3.0, 3A and
3B gates.

---

## 11 · After the 2026-11-15 fixes and decisions

What changed — PHASES, "Owner verification (2026-11-15) and fixes" and D38–D48:

* join hints: a claim to a key of another type fails instead of erroring (D1);
  model hints expire (`stale`) or are `refuted` by their test (D2, migration
  `0006`); the object list names unique keys (D11);
* evidence: chance passes on nearly dense keys are weak (D3); `phone` before
  `numeric_text` (D6); blank text is NULL (D7); Stage 3 samples values without
  bias (D8); an FK no longer contradicts a `code`, and is neutral to a
  `dimension`, `flag` or `timestamp` (D40);
* profiler: the sample first, columnstore column by column, low-cardinality
  top-k of large tables in one `GROUPING SETS` scan, every `partial` table says
  why (D4, D5); tables profiled earlier meet parents profiled later (D12);
* reasoning: pass-through view columns inherit and are never queued; stored
  answers are re-scored without the model when only the scorer's inputs
  changed; `make rescore` never calls the model (D43, D44).

Steps 11a–11d call no LLM.

### 11a · Migrate and test

```bash
cd backend && ../gaia-env/bin/alembic upgrade head && cd ..   # 0005 -> 0006
make test                                   # with the §9 exports incl. GAIA_TEST_MSSQL=1
make test ARGS="tests/integration/test_mssql_target_live.py -k large_table -s"
```

- [ ] `alembic` ends with `Running upgrade 0005 -> 0006, Join hints: status ``refuted`` …`
- [ ] `make test`: **378 passed, 0 skipped** (Ollama up; without: 376 passed, 2 skipped).
      `test_profiler_on_a_large_table_records_every_degradation` passes now — send its
      `-s` output (`dw.sales_wide`: status, time, queries, notes)

### 11b · Profile both databases again, one run each

`--force` re-profiles every table: blank text, the new statements and the join
candidates (new specificity rule, and every table paired with every parent).

```bash
make profile TARGET=demo_pg ARGS="--force --report" > verification/11_profile_pg.out
make profile ARGS="--force --report"             > verification/11_profile_mssql.out   # ~5-8 min
```

- [ ] `demo_pg`: `{'profiled': 24, 'skipped': 4}`, candidates `tested=17 kept=8 passed=6` as before
- [ ] `demo_mssql`: `dbo.transactions` and `dbo.transaction_lines` should now be `profiled`
      (their low-cardinality columns share one `GROUPING SETS` scan; if that statement
      times out, a note says so and they fall back to one statement each — untested on SQL
      Server, send it either way); `dw.fact_sales` and `dw.sales_wide` (columnstore) run one
      statement per column, no `batched scan not run`; any `partial` table lists its
      reasons under it
- [ ] the candidates no longer keep `agg_product_performance.line_count -> hr.payroll_lines…`
      or `agg_sales_by_month.units -> …customer_key` (`(not kept)  (weak: dense integer key)`)
- [ ] `footprint`: send the list (the new statements may create a few statistics)

### 11c · Test the claims again

The claims that errored on your host are still *pending* (nothing was recorded
for them):

```bash
make profile-tests TARGET=demo_pg ARGS="--schema legacy --schema rpt" > verification/11_tests_pg.out
make profile-tests ARGS="--table dbo.transactions --with-neighbours" > verification/11_tests_mssql.out
```

- [ ] `demo_pg`: `f2 inc:crm.customers.customer_id FAIL metric=0.0`, `f9 inc:store.stores.store_id
      FAIL metric=0.0`; `join hints : {'refuted': 2}` (or fewer if they were staled by a newer
      answer); no `notes:` line with `failed`
- [ ] `demo_mssql`: `dbo.transaction_lines.condition inc:dw.dim_condition.condition_key FAIL
      metric=0.0`, `join hints : {'refuted': 1}`

### 11d · Re-score without the model

```bash
make rescore TARGET=demo_pg ARGS="--schema legacy --schema rpt --schema hr" > verification/11_rescore_pg.out
make rescore ARGS="--table dbo.transactions --with-neighbours"           > verification/11_rescore_mssql.out
make review TARGET=demo_pg > verification/11_review_pg.out
make review                > verification/11_review_mssql.out
```

- [ ] `llm : calls=0`; tables `rescored` (a table without a usable stored answer says
      `needs_reasoning`)
- [ ] the queue no longer holds pass-through view columns (`rpt.v_legacy_transactions.
      customer_code`, `…transaction_id`, …) and `legacy.tb_mst01.c07` is no longer `abstain`
- [ ] `payments.currency_code`, `trade_ins.currency_code`, `payments.method`,
      `trade_ins.source_system` carry no `contradiction` tag

### 11e · Reason where the model would see something new

```bash
make reason-dry TARGET=demo_pg ARGS="--schema legacy --schema rpt"
make reason     TARGET=demo_pg ARGS="--schema legacy --schema rpt --cost-report" > verification/11_reason_pg.out
make reason-dry ARGS="--table dbo.transactions --with-neighbours"
make reason     ARGS="--table dbo.transactions --with-neighbours --cost-report"  > verification/11_reason_mssql.out
make review TARGET=demo_pg > verification/11_review2_pg.out
make review                > verification/11_review2_mssql.out
```

- [ ] the dry runs list `would_reason (evidence changed)` only for tables whose shown
      evidence changed (blank text, new or failed tests, new candidates); everything else
      `would_rescore` or `skipped`; views `… N pass-through column(s) inherit`
- [ ] `reasoned=… failed=0`; a view with computed columns sends only those to the model

Then send the files above and:

```sql
-- hint lifecycle
SELECT from_table, from_columns, to_table, to_columns, status
FROM semantics.join_hint WHERE source = 'llm' ORDER BY 1, 2;
-- tests recorded as not run (should be none, or each with a reason)
SELECT column_name, test_id, detail FROM profiling.verification_test
WHERE is_current AND method = 'not_run';
-- inherited view columns
SELECT t.object_name, k.column_name, c.score_breakdown->>'inherited_from' AS base,
       round(c.calibrated_score::numeric, 3) AS score
FROM semantics.column_semantic c JOIN catalog.columns k ON k.id = c.column_id
JOIN catalog.tables t ON t.id = c.table_id
WHERE c.is_current AND c.score_breakdown ? 'inherited_from' ORDER BY 1, 2;
```

---

## 12 · After the §11 review (no LLM)

Owner run of §11: done except §11e (re-reasoning; deferred — gateway slow) and its
SQL. Three fixes followed (PHASES, "Owner run of §11"): a refuted parent no longer
contradicts a reference found in another key (D49: `tb_trx02.f2`, `f9`); integer
join specificity uses the key density where the values lie (D3b:
`line_count -> payroll_line_id`); columnstore reads distinct counts with the
column's aggregates (D4b: `dw.sales_wide`).

```bash
make test                                    # §9 exports incl. GAIA_TEST_MSSQL=1
make profile ARGS="--force --report" > verification/12_profile_mssql.out
make rescore TARGET=demo_pg ARGS="--schema legacy --schema rpt --schema hr" > verification/12_rescore_pg.out
make rescore ARGS="--table dbo.transactions --with-neighbours"           > verification/12_rescore_mssql.out
make review TARGET=demo_pg > verification/12_review_pg.out
make review                > verification/12_review_mssql.out
```

- [ ] `make test`: **388 passed, 0 skipped**
- [ ] `12_profile_mssql.out`: `agg_product_performance.line_count -> hr.payroll_lines.payroll_line_id`,
      `…units_sold -> …` and `agg_sales_by_month.line_count / units -> …customer_key` are
      `(not kept)  (weak: dense integer key)`; if `dw.sales_wide` is still `partial`, its
      `distinct of N column(s) not exact` note names fewer than the 25 columns of §11
      (with the adaptive time rule, D50, it should now be `profiled`)
- [ ] `12_rescore_pg.out`: every table `rescored` (scorer v2.2), `calls=0`
- [ ] `12_review_pg.out`: `legacy.tb_trx02.f2` and `f9` are no longer in the queue

§11e (`make reason` on both targets) can follow whenever the gateway allows; it
is not needed for the 3A gate.

---

## 13 · Phase 3C: the evaluation

The harness is built (PHASES, "3C harness"). Nothing here writes to the metadata
store; answers go to `evaluations/<name>/` (git-ignored; every model answer is
kept there, so an interrupted run simply resumes). Relative paths are from the
repository root.

### 13a · Your labels (~1 h, before the named conditions)

```bash
make evaluate-plan   ARGS="--table dbo.transactions --with-neighbours"
make evaluate-labels ARGS="--table dbo.transactions --with-neighbours --file verification/labels_nge_small.csv"
```

Fill `meaning` (what the column holds, in business terms — your words, not the
column name) and `role` (`identifier`, `reference`, `measure`, `dimension`,
`timestamp`, `flag`, `code`, `free-text`; several with `|`, any with `*`) for the
40 rows. Leave a row's `meaning` empty to skip it.

### 13b · The runs (overnight: ~170–200 calls for nge_small)

Criteria 1 and 3 need only the name-blind conditions, so they come first:

```bash
make evaluate ARGS="--out evaluations/nge_small --table dbo.transactions --with-neighbours --condition B-C --condition B-P"
make evaluate ARGS="--out evaluations/nge_small --table dbo.transactions --with-neighbours --condition N-C --condition N-P --condition B-P-nt"
make evaluate TARGET=demo_pg ARGS="--out evaluations/demo_pg --schema legacy --condition N-P --condition N-P-h --condition B-P --hide rpt.v_legacy_transactions"
```

- [ ] each condition prints `… column(s), N call(s)` and no `FAILED`; a repeated command
      prints `0 new` calls (resumed)

### 13c · Judge, audit, report

```bash
make evaluate-judge  ARGS="--out evaluations/nge_small --labels verification/labels_nge_small.csv"
make evaluate-audit  ARGS="--out evaluations/nge_small"        # fill owner_meaning: yes/partial/no
make evaluate-report ARGS="--out evaluations/nge_small --labels verification/labels_nge_small.csv"
make evaluate-judge  ARGS="--out evaluations/demo_pg --labels infra/pg-target/labels.csv"
make evaluate-report ARGS="--out evaluations/demo_pg --labels infra/pg-target/labels.csv"
```

The audit file does not show the judge's verdict — judge each row yourself: does
the answer mean what the reference means? Run `evaluate-report` again after
filling it. Send both `report.md` files (and the audit file).

### 13d · Criterion 5: a production run, costed

When the gateway allows (the longest step: all 71 objects, ~140 calls):

```bash
make reason ARGS="--cost-report" > verification/13_reason_all.out
make evaluate-cost ARGS="--file verification/13_cost_mssql.md"
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `unresolved env placeholders: GAIA_PG_TARGET_PASSWORD` | missing in `.env` | §1 |
| `pg-target-seed`: `set GAIA_PG_TARGET_ADMIN_URL` | missing in `.env` | §1 |
| `relation "semantics.evidence_item" does not exist` | migration `0004` not applied | §1 `alembic upgrade head` |
| `security.read_only: inconclusive …` | the DDL attempt failed for a non-permission reason (timeout, dropped connection) | re-run; send the line if it persists |
| `StatementTimeout` in `introspect` | a very slow catalog | raise `targets.<name>.limits.statement_timeout_s` |
| replay reports `awaiting an answer` | your prompts differ from the sandbox's | §6a note |
| `relation "profiling.table_profile" does not exist` | migration `0005` not applied | §1 `alembic upgrade head` |
| `REFUSED: profiling is disabled for 'demo_mssql'` | `profiling.enabled: false` or `targets.<name>.profiling_enabled: false` | set it to `true` (§8) |
| `REFUSED: outside the profiling window` | `profiling.window` is set | run in the window or add `--ignore-window` |
| `REFUSED: another profiling run holds the lock` | a second `make profile` on the same target | wait; a crashed run releases it when its connection closes |
| views' lineage `unresolved: no definition` on SQL Server | the login lacks `VIEW DEFINITION` | `infra/sqlserver/001_readonly_login.sql` grants it; re-run that grant |
| a test file fails with an `AssertionError` inside `pytest_asyncio` | it was run from `backend/` with a path | run through `make test ARGS=...` (from the repo root) |
| `make measure` takes long | large tables, cold cache | it stops starting new work at `--max-minutes` (90); partial reports are still useful |
