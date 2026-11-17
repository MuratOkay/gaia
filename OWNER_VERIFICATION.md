# Owner verification — what to run on your machine

Everything so far was built and tested in a sandbox that has PostgreSQL 16 —
since 2026-11-16 a full reproduction of the PostgreSQL side: `demo_pg` seeded at
scale 1 (the same 2,973,244 rows as on your host) and a metadata store with
pgvector — but **no SQL Server, no Docker, no Ollama and no LLM gateway**.
This file lists what only your machine can verify, the exact commands, what you
should see, and what to send back.

## How this file works from 2026-11-16 (D52, D56)

PostgreSQL is the main dialect (D52). What I can run myself on PostgreSQL, I run
before I hand anything over. What only you can run — SQL Server, the LLM gateway,
Ollama — is listed below as **asynchronous items**: run them when you can, send
the outputs, and I triage them when they arrive, before new work goes on top.

* SQL Server items never block, with one exception: anything that shows a write
  path or a guard bypass (safety, D52 b).
* **Run them on the latest delivered build** (D56). Every delivery is a commit;
  start every output file with `make stamp` — it prints the build, the sqlglot
  version and the git commit — so each result is read against the code that
  produced it. A finding is fixed in a later build and the item is run again
  there. *Latest build: `2026-11-16 · D65 (4F)`; it contains every fix the open
  items below were written for.*
* Sections 1–13 are history (done, or superseded where §14/§15 say so).
* **2026-11-17: the D65 outputs are triaged — §21.** The Phase 4 gate numbers are in
  (§18b: executable 100 %, correct 97 %). §21 lists what was confirmed, what was found, the
  few runs still wanted and three questions for you.

### Run plan — what depends on what (2026-11-16)

Almost nothing has to wait for anything else. The real dependencies are three:

1. **Once per delivery, first:** the new dependencies (`make venv`), the migrations
   (`alembic upgrade head`), and — since 4F — a `GAIA_MASTER_KEY` in `.env`. One `make test`
   on the latest build replaces every earlier section's test step (§15a–§19a).
2. **Freeze 3C before profiling SQL Server again.** The rest of 3C must see the evidence
   B-C and B-P saw. Since this build the inputs are frozen into `evaluations/nge_small`
   (`make evaluate-freeze`, seconds, no model); after that, `make profile` on SQL Server
   (S2, S3) is free to run at any time.
3. **Judge a 3C condition after it has run** (judging is per condition, so B-C and B-P can be
   judged today).

Everything else runs in parallel, in three tracks that wait on different things:

| Track | Waits on | Items | Time |
|---|---|---|---|
| `quick` | the databases (and Ollama for retrieval) | freeze 3C inputs; §15 queries on both targets; §16 retrieval on both; S2/S3 SQL Server profile; data sources list | ~20 min |
| `gateway` | the LLM gateway, **thinking off** (fast calls) | §18b golden run; 3C judging of B-C/B-P and its report (criteria 1 and 3); S7 two questions | 1–2 h |
| `gateway-long` | the LLM gateway, **thinking on** (slow calls) | 3C N-C, N-P, B-P-nt; the `demo_pg` 3C run; judging and reports; §14d `demo_pg` meanings | overnight |
| by hand | you | §18c a clarification; §19b the chat API (`make dev-api`) | 15 min |

`scripts/owner_runs.sh setup | quick | gateway | gateway-long` runs a track unattended:
every step writes `verification/20_<step>.out` starting with `make stamp`, a failing step
does not stop the next, and every LLM step is resumable (run the track again). `quick` can
run beside either gateway track. Two gateway tracks at once must share the gateway's 4 slots:
`GAIA_LLM_CONCURRENCY=2` in front of each. Better numbers come later for free: after §14d,
re-running §16b and §18b measures retrieval and answers with full meanings.

What I read first when it arrives: `20_golden_pg.out` (the Phase 4 gate), then
`20_report_nge.out` (3C criteria 1 and 3), then the rest in any order.

### SQL Server track (D56) — asynchronous, non-blocking except safety

| Id | What | Where | Written for | Status | What a result changes |
|---|---|---|---|---|---|
| S1 | `make test` with `GAIA_TEST_MSSQL=1` (8 live tests; 2 new for 4A) | §15a | D57 | **done for SQL Server** (D65, `all_test_latest.out`: 634 passed, **2 failed**, 0 skipped — every SQL Server live test passed; the 2 failures are 4F PostgreSQL tests, §21 T1) | a failing live test → a SQL Server fix in a later build |
| S2 | D54 (neighbour control) on the counts: `line_count`, `units_sold`, `units` | §14c | D55 | **done** (D65, `14_profile_mssql.out`): all four `(not kept) (weak: dense integer key)`, neighbours 94–99 % | the 3A SQL Server note |
| S3 | `dw.sales_wide` stays `partial` | §14c | D55 | **done — better than expected**: `profiled` (47 statements, 128 s); `dw.fact_web_activity` is now the one `partial` (one column scan timed out) | nothing unless it fails |
| S4 | 4A on SQL Server: guarded queries on `nge_small`, refusals | §15c | D57 | open (not run yet) | T-SQL guard or executor; a bypass is **safety** |
| S5 | 3C on `nge_small` (SQL Server data, LLM-bound): ~~resume B-C/B-P~~ **done 2026-11-16**; ~~N-C, N-P~~ **done**; B-P-nt, judge, audit, report | §13b–d | D55 | open — B-P-nt **incomplete** (7 calls `http 400`, §21 T4); a model change mid-run to check (§21 T3) | the 3C gate |
| S6 | Retrieval on `nge_small`: correct the draft questions, then `make retrieval-eval` (reads only Gaia's store) | §16c | D59 | open (not run yet) | retrieval defaults; the golden set |
| S7 | Questions answered on `nge_small` with the configured model | §17c | D60 | **done** (D65, `17_ask_mssql.out`): both answered in T-SQL, none failed; the 2024 question returned no rows — §21 Q2 | the T-SQL prompt rules; 4D on SQL Server |
| S8 | The `nge_small` golden run, once its questions exist (P4-1) | §18d | D63 | waiting for the questions | the Phase 4 numbers on SQL Server |

### Other owner items

| Id | What | Where | Blocks | Status (triage 2026-11-17, §21) |
|---|---|---|---|---|
| L1 | `demo_pg` semantics for every object (`make reason TARGET=demo_pg`) | §14d | Phase 4 step 4D (the golden run), not 4B/4C | **done**: 28/28 reasoned, `failed=0` — but the second run re-reasoned 24 tables (§21 T2, Q1) |
| L2 | 4A on your PostgreSQL 18 host: migrate, the example queries (the D48 hint join with your model's real hints) | §15b | nothing (verified on PostgreSQL 16 here) | **done**: every item as expected |
| L3 | Decisions P4-1 … P4-9 (PHASES, Phase 4) | §15d | 4C/4D | settled except P4-1 for `nge_small` (asynchronous) |
| L4 | Optional: a realistic PostgreSQL copy of `nge_small` | §14f | nothing | not requested |
| L5 | Retrieval on `demo_pg` with your meanings and `bge-m3` (after L1 for the full picture) | §16b | nothing; its numbers set the retrieval defaults | open — the last Phase 4 acceptance number (a trial) |
| L6 | The first real-model answers on `demo_pg`: five golden questions and a clarification | §17b | nothing (optional since §18b, which covers it) | **done**: 3 of 4 numbers right (g07 wrong, as in §18b); the `--clarified` follow-up asked again (§21 T6) |
| L7 | **The golden run of `demo_pg` with the configured model** — the Phase 4 acceptance numbers | §18b | the Phase 4 gate | **done**: executable **100 %**, correct **97 %** (29/30) |
| L8 | A clarification answered by you, and its record (D61) | §18c | nothing | **done** (`agrees`); the answer's own output is missing from the file |
| L9 | The chat API with the configured model: a question, a follow-up, a clarification | §19 | nothing | **done**: every item as expected |
| L10 | A data source added at runtime: master key, add, probe, use (4F) | §20 | nothing | open (not run yet); the 4F tests: §21 T1 |

Done on 2026-11-16: D52 refinements accepted; gates 3.0, 3A (PostgreSQL), 3B ticked;
Phase 4 approved (4A first).

Work through the numbered sections in order when you do run them; each lists
its expected results. Tick the boxes as you go. Times are rough.

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
| 14 | After the 2026-11-16 fixes (D52–D55): tests, resume 3C, SQL Server checks (async), decisions | LLM gateway (14b, 14d), `sql1` (14c) | 20 min + LLM |
| 15 | Phase 4A: migrate, tests, the SQL guard on both targets | metadata DB, `demo_pg`, `sql1` (15c) | 15 min |
| 16 | Phase 4B: tests, retrieval on both targets | metadata DB, Ollama | 10 min |
| 17 | Phase 4C: tests, the first questions answered by the configured model | LLM gateway, Ollama | 20–30 min |
| 18 | Phase 4D: migrate, tests, the golden run, a clarification of yours | LLM gateway, Ollama | 45–90 min, mostly unattended |
| 19 | Phase 4E: migrate, tests, the chat API | LLM gateway, Ollama | 15 min |
| 20 | Phase 4F: the master key, a data source added at runtime; the run plan's tracks | metadata DB, `demo_pg` | 10 min (+ the tracks) |
| 21 | Triage of the D65 outputs (2026-11-17): findings, the runs still wanted, three questions | metadata DB, `demo_pg`, Ollama; `sql1` for S4/S6 | ~30 min + answers |

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

*Triage note (2026-11-17): the output pasted above is `demo_pg`'s (port 5434). The SQL Server
result the two ticks rest on is `verification/4_check_target_mssql.out`: 9 × PASS, `nge_small`,
`write denied as expected in schema dbo (error 262)`.*

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
- [x] columns with a key, FK, constraint, detector hit or passed test can reach
      `accepted`; names alone cannot (they cap at 0.65) *(2026-11-15: `8_reason.out` 48 of
      99 accepted — the first half. The ceiling is the scorer's, proven by
      `test_scoring_v2.py` (a perfect names-only answer scores 0.650, never accepted) and
      passed on your host with the suite; `query1_result.csv`, topping out at 0.623, is
      consistent with it)*

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

*Superseded (triage 2026-11-17): `all_tests.out` gave 359 passed, 6 skipped (the SQL Server
live tests did not run then). Every later build ran the full suite with 0 skipped — 378 (§11a),
401 (D55), and 634 passed + 2 failed on D65 (§21 T1) — so the boxes below are answered by those
runs.*

- [x] ~~**365 passed, 0 skipped**~~ superseded — see the note above
- [x] In particular these ran (use `make test ARGS="-rA -q"` to list them) — *all of them ran
      with 0 skipped from §11a on:*
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

- [x] `alembic` ends with `Running upgrade 0005 -> 0006, Join hints: status ``refuted`` …`
      *(not in a file; 0006 is applied — every later migration up to 0010 runs on it)*
- [x] `make test`: **378 passed, 0 skipped** (Ollama up; without: 376 passed, 2 skipped).
      `test_profiler_on_a_large_table_records_every_degradation` passes now — send its
      `-s` output (`dw.sales_wide`: status, time, queries, notes)
      *(`11a_make_test.out`: 378 passed; `11a_large_table.out`: `partial`, 61.6 s, 17 queries,
      every degradation noted)*

### 11b · Profile both databases again, one run each

`--force` re-profiles every table: blank text, the new statements and the join
candidates (new specificity rule, and every table paired with every parent).

```bash
make profile TARGET=demo_pg ARGS="--force --report" > verification/11_profile_pg.out
make profile ARGS="--force --report"             > verification/11_profile_mssql.out   # ~5-8 min
```

- [x] `demo_pg`: `{'profiled': 24, 'skipped': 4}`, candidates `tested=17 kept=8 passed=6` as before
      *(`11_profile_pg.out`, exactly; footprint `213 -> 213, new: 0`)*
- [x] `demo_mssql`: `dbo.transactions` and `dbo.transaction_lines` should now be `profiled`
      (their low-cardinality columns share one `GROUPING SETS` scan; if that statement
      times out, a note says so and they fall back to one statement each — untested on SQL
      Server, send it either way); `dw.fact_sales` and `dw.sales_wide` (columnstore) run one
      statement per column, no `batched scan not run`; any `partial` table lists its
      reasons under it *(`11_profile_mssql.out`: both `profiled` with 4 statements each;
      `dw.fact_sales` 38 statements, no `batched scan not run`; `dw.sales_wide` the one `partial`,
      with its notes)*
- [ ] the candidates no longer keep `agg_product_performance.line_count -> hr.payroll_lines…`
      or `agg_sales_by_month.units -> …customer_key` (`(not kept)  (weak: dense integer key)`)
      *(not met in §11: still kept at 94–99 % — D3b, then D54; **met on D65** in §14c)*
- [x] `footprint`: send the list (the new statements may create a few statistics)
      *(`411 -> 424, new: 13`)*

### 11c · Test the claims again

The claims that errored on your host are still *pending* (nothing was recorded
for them):

```bash
make profile-tests TARGET=demo_pg ARGS="--schema legacy --schema rpt" > verification/11_tests_pg.out
make profile-tests ARGS="--table dbo.transactions --with-neighbours" > verification/11_tests_mssql.out
```

- [x] `demo_pg`: `f2 inc:crm.customers.customer_id FAIL metric=0.0`, `f9 inc:store.stores.store_id
      FAIL metric=0.0`; `join hints : {'refuted': 2}` (or fewer if they were staled by a newer
      answer); no `notes:` line with `failed` *(`11_tests_pg.out`, exactly)*
- [x] `demo_mssql`: `dbo.transaction_lines.condition inc:dw.dim_condition.condition_key FAIL
      metric=0.0`, `join hints : {'refuted': 1}` *(`11_tests_mssql.out`, exactly)*

### 11d · Re-score without the model

```bash
make rescore TARGET=demo_pg ARGS="--schema legacy --schema rpt --schema hr" > verification/11_rescore_pg.out
make rescore ARGS="--table dbo.transactions --with-neighbours"           > verification/11_rescore_mssql.out
make review TARGET=demo_pg > verification/11_review_pg.out
make review                > verification/11_review_mssql.out
```

- [x] `llm : calls=0`; tables `rescored` (a table without a usable stored answer says
      `needs_reasoning`)
- [x] the queue no longer holds pass-through view columns (`rpt.v_legacy_transactions.
      customer_code`, `…transaction_id`, …) and `legacy.tb_mst01.c07` is no longer `abstain`
- [x] `payments.currency_code`, `trade_ins.currency_code`, `payments.method`,
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

- [x] the dry runs list `would_reason (evidence changed)` only for tables whose shown
      evidence changed (blank text, new or failed tests, new candidates); everything else
      `would_rescore` or `skipped`; views `… N pass-through column(s) inherit`
- [x] `reasoned=… failed=0`; a view with computed columns sends only those to the model

*Owner run, 2026-11-16 (`verification/11e_*`, `11_reason_mssql.out`, `11_review2_*`):
both targets `reasoned=0 rescored=0 skipped=8 / 10 failed=0`, `calls=0` — correct: the
§12 re-scores had already applied everything, and nothing the model would see had
changed (on `demo_pg` only `hr.departments`' evidence changed, outside this scope).
Hint lifecycle as designed: wrong parents `refuted` (`f2 → customer_id`, `f9 → store_id`,
`condition → condition_key`), `v_legacy_transactions` hints `stale` (its pass-through
columns now inherit), `f2 → customer_code` and `f9 → store_code` `inferred`. No test
recorded as not run. Seven of eight `v_legacy_transactions` columns inherit (the
eighth, `transaction_date`, is computed). Queues: no pass-through view column, `c07` not
`abstain`, none of the four FK code columns tagged `contradiction`. Also received:
`12_alltests_new.out`, **401 passed, 0 skipped** — the D55 build with every live test,
SQL Server included (S1 for that build).*

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

*Reviewed 2026-11-16 (PHASES, "Owner runs of §12 and §13b"); the boxes record that verdict.*

- [x] `make test`: **388 passed, 0 skipped** — *`12_all_tests.out` 380 (older code than §12 was
      written for); `12_alltests_new.out` **401 passed, 0 skipped** on D55*
- [ ] `12_profile_mssql.out`: `agg_product_performance.line_count -> hr.payroll_lines.payroll_line_id`,
      `…units_sold -> …` and `agg_sales_by_month.line_count / units -> …customer_key` are
      `(not kept)  (weak: dense integer key)`; if `dw.sales_wide` is still `partial`, its
      `distinct of N column(s) not exact` note names fewer than the 25 columns of §11
      (with the adaptive time rule, D50, it should now be `profiled`) — *not met here (still
      kept; `sales_wide` partial with 20) → D54; **both met on D65** in §14c*
- [x] `12_rescore_pg.out`: every table `rescored` (scorer v2.2), `calls=0` — *9 rescored, 1
      `needs_reasoning` (`hr.employees`, never reasoned then), `calls=0`*
- [x] `12_review_pg.out`: `legacy.tb_trx02.f2` and `f9` are no longer in the queue (D49 confirmed)

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

- [x] each condition prints `… column(s), N call(s)` and no `FAILED`; a repeated command
      prints `0 new` calls (resumed)

*Owner run, 2026-11-15/16: the first command stopped — B-P `FAILED 1`
(`dbo.customer_addresses`, truncated answer), and a B-C critique had failed without
being reported. Both fixed (D53, D55); resume with §14b, then continue here.*

*Owner runs of the second command (triage 2026-11-17; no `make stamp` in either file):
`13b_evaluate_mssql_nc_np.out` — **N-C 99 columns, 20 calls; N-P 99 columns, 24 calls, second
round 2**, no failure (the B-P-nt part of that run is not in the file). `13b_evaluate_nc_np.out`
— the same command again: N-C and N-P fully replayed (0 new), but **B-P-nt incomplete: 38
columns, `FAILED 5` (`semantics: completion failed: http 400` on `customer_addresses`,
`payments`, `transaction_lines`, `transactions`, `employment_spells`) and `CRITIQUE FAILED 2`
(`shops`, `trade_ins`)**. A 400 is the gateway refusing the request, not a capacity error, and
the harness kept only `http 400`, not the gateway's reason (§21 T4). Don't re-run B-P-nt until
T4 is fixed — the same 400s would come back, with nothing more to learn from them. The
`demo_pg` command (third line) has not run yet.*

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

- [x] *`13_reason_all.out` (triage 2026-11-17): all 71 objects — `reasoned=68 rescored=3 failed=0`,
      159 calls, 0.90 M tokens in / 1.05 M out (0.73 M of them thinking), **55 min wall**;
      D53 split the six tables that truncated (`customer_addresses`, `transaction_lines`,
      `transactions`, `fact_sales`, `fact_web_activity`, `sales_wide` in 4 chunks) and every one
      completed. 607 columns: 231 accepted, 263 deep_profile, 91 flagged, 22 abstain; queue 289.
      Model `deepseek-flash` (§21 T3).*
- [ ] `make evaluate-cost` — not run yet (`13_cost_mssql.md` missing); it reads only the store,
      so it can run any time

---

## 14 · After the 2026-11-16 fixes (D52–D55)

What changed — PHASES, "Owner runs of §12 and §13b (2026-11-15/16) and fixes":

* **D53** — a truncated answer is asked again in smaller column chunks (a
  truncated critique in halves). The cause in your run was thinking, not width:
  12,437 of the 16,000 output tokens were the model's chain of thought on a
  14-column table. **Do not raise `llm.max_output_tokens`**: at this gateway's
  ~15 tokens/s a bigger budget outlasts the 1,800 s read timeout, and it changes
  every request key, so your 37 recorded answers would all be asked again.
* **E2** — a failed critique is now reported (`CRITIQUE FAILED`, exit 2). In your
  B-C run the critique of `dbo.payments` had failed (a gateway error) without a word.
* **D55** — the report compares two conditions on the columns both answered.
* **D54** — the neighbour control for integer joins: §12 showed D3b did not stop
  `line_count → payroll_line_id` on SQL Server (reproduced and explained in PHASES).
* `make stamp` prints the build; start every output with it.

### 14a · Build and tests (superseded by §15a)

```bash
make stamp                                   # gaia build 2026-11-16 · D55
make test > verification/14_tests.out        # with §9's exports, incl. GAIA_TEST_MSSQL=1
```

- [x] *Superseded by §15a* (the latest build has more tests); kept for the record:
      `gaia build 2026-11-16 · D55`, 401 passed, 0 skipped.

### 14b · Resume the 3C runs (S5) — before any new `make profile` on SQL Server

The conditions read the stored Stage 1 evidence. A new profile run would change
what B-P shows the model, so its recorded answers would no longer match and every
B-P call would be asked again. So: **no `make profile` on `demo_mssql`, and no change
to `llm.*` or the prompts, until the `nge_small` runs of §13b are finished.**

```bash
(make stamp; make evaluate ARGS="--out evaluations/nge_small --table dbo.transactions \
  --with-neighbours --condition B-C --condition B-P") > verification/14_BC_BP.out 2>&1
```

- [x] `B-C      99 column(s), 1 new call(s), 19 replayed` — the `dbo.payments` critique
      asked again — and no `CRITIQUE FAILED`. (If the gateway fails it again:
      `CRITIQUE FAILED 1`, exit 2; run the same command again.)
- [x] `B-P 99 column(s)`, no `FAILED`. `dbo.customer_addresses` is asked once more with
      the same prompt (~15 min; its truncated answer was not recorded by the old code).
      If it truncates again you see
      `dbo.customer_addresses: answer truncated on 14 column(s): asking again with at most 7 column(s) per chunk`
      and two chunk calls plus their critiques. A second round may follow if its claim
      tests change its evidence.
- [x] `B-P      99 column(s), N new call(s), 18 replayed` (N = 2 if the table fits this time,
      ~5 if it is split, a few more with a second round)
- [x] last line `model calls: N new, 37 replayed` *(`13b_evaluate_mssql_bc_bp.out`: `5 new, 37
      replayed`)*
- [x] the same command once more: `0 new` everywhere *(D65, `14_BC_BP.out`: `inputs: frozen in
      …/inputs.json (the store is not read)`; B-C 20 and B-P 22 replayed, **`0 new, 42 replayed`** —
      also the first proof on your host that the frozen inputs (D66) reproduce B-P's prompts
      byte for byte)*

*Triage note (2026-11-17): the numbers quoted in the next paragraph are in
`13b_evaluate_mssql_bc_bp.out`; `14_BC_BP.out` now holds the D65 re-run above.*

*Owner run, 2026-11-16 (`verification/14_BC_BP.out`): `B-C 99 column(s), 1 new call(s), 19
replayed, 228.9 s`; `B-P 99 column(s), 4 new call(s), 18 replayed, 6412.6 s; second round: 1`;
`model calls: 5 new, 37 replayed` — as expected: `dbo.customer_addresses` fitted this time (no
split needed), Stage 3 changed one table's evidence, so one second round. No FAILED, no
CRITIQUE FAILED. The four B-P calls took ~27 minutes each: the gateway, not the pipeline, is
what makes 3C slow. Criteria 1 and 3 can be computed after judging (§13c).*

Then the rest of §13b (N-C, N-P, B-P-nt; the `demo_pg` run), §13c and §13d as written.

### 14c · SQL Server, asynchronous, non-blocking (S2, S3)

**After** the `nge_small` runs of §13b:

```bash
(make stamp; make profile TARGET=demo_mssql ARGS="--force --report") > verification/14_profile_mssql.out 2>&1
```

- [x] `agg_product_performance.line_count` and `.units_sold -> hr.payroll_lines.payroll_line_id`,
      and `agg_sales_by_month.line_count` / `.units -> dw.agg_customer_ltv.customer_key`:
      `(not kept)  (weak: dense integer key)  [neighbours NN%]` with NN close to the
      hit rate. *If one is still kept*, its `[neighbours NN%]` says why — send the line.
      *(D65: all four not kept — hits 99/99/96/94 %, neighbours 98/99/95/98 %)*
- [x] genuine integer references are still kept, e.g. `dw.sales_wide.date_key ->
      dw.dim_date.date_key` with a low neighbour rate (nearby integers of a `yyyymmdd` key
      are mostly not dates; the sandbox's date-key test gives 31 %). References into dense
      surrogate ids may turn `(weak …)`: that is the documented limit of dense keys (3A),
      not a regression. Send the kept pairs that changed (compare with `12_profile_mssql.out`)
      *(every `date_key -> dim_date` kept, neighbours 29–36 %; `fact_web_activity.customer_key ->
      web.accounts.customer_id` kept at 38 %. Compared with §12, exactly **9 pairs** changed, all
      from kept to not kept: the four counts above, `fact_web_activity.customer_key ->
      agg_customer_ltv.customer_key` (97 %, neighbours 93 % — a dense surrogate id, the documented
      limit) and **four `session_id -> page_view_id` pairs (neighbours 100 %)** — false positives
      D54 removed without being written for them. Kept: 61 → 52. Footprint `426 -> 426, new: 0`)*
- [x] `dw.sales_wide`: still `partial` is acceptable (its metrics come from the sample and
      say so); send its notes *(better: **`profiled`**, 47 statements, 128 s. The one `partial` is
      now `dw.fact_web_activity`: the scan of `url_path` hit the 60 s statement timeout, so its
      metrics come from the sample and say so — `profiled` in §12 at 65 s, a cold-cache
      difference, acceptable under the same rule)*

### 14d · `demo_pg` semantics for Phase 4 (L1)

Phase 4's golden run (step 4D) needs current meanings for every `demo_pg` object.
Overnight, whenever the gateway allows:

```bash
(make stamp; make reason TARGET=demo_pg ARGS="--cost-report") > verification/14_reason_pg.out 2>&1
(make stamp; make profile-tests TARGET=demo_pg; make reason TARGET=demo_pg) > verification/14_reason_pg_2.out 2>&1
```

- [x] `failed=0`; truncations, if any, end in chunks (`answer truncated … asking again`)
      *(D65, `14_reason_pg.out`: **28/28 reasoned, `failed=0`**, 56 calls, 17 min; 213 columns:
      108 accepted, 75 deep_profile, 27 flagged, 3 abstain; the embeddings failed (Ollama
      `ReadTimeout`) and were written by the second run (241). `14_reason_pg_2.out`: Stage 3 had
      nothing to test (`tables : {}`), yet the second `make reason` re-reasoned **24 tables with
      46 calls** — it should have made none — and `crm.customers` and `sales.orders` truncated
      there and were split into chunks. Probably the two runs overlapped; see §21 T2 and Q1)*

### 14e · Decisions — answered 2026-11-16 (refinements accepted, gates ticked, 4A started); P4 → §15d

1. **D52 refinements** (PHASES, decisions log): (a) offline T-SQL tests stay blocking;
   (b) safety findings on SQL Server are never non-blocking; (c) the asynchronous ledger
   above; (d) accuracy claims stay on realistic data. Accept, change or reject each.
2. **Phase 3 gates**: 3.0, 3B and 3A are ready for your tick (PHASES, Phase 3 "Gate").
3. **Phase 4**: P4-1 … P4-9 (PHASES, Phase 4 "Decisions needed") and approval to start.
4. **P4-8**: switch `app.default_target` to `demo_pg` when Phase 4 starts? From §14 on,
   every SQL Server command names `TARGET=demo_mssql` explicitly, so nothing here breaks.

### 14f · Optional: a realistic PostgreSQL copy of `nge_small` (L4)

`demo_pg` proves the machinery; I designed it, so it cannot prove accuracy. A
PostgreSQL copy of `nge_small` (e.g. with `pgloader` from the restored SQL Server
database into a new database on the `gaia-target-pg` server) would put real data
in the main dialect: your labels would carry over, 3C and Phase 4's golden set
could run on PostgreSQL. It is a copy you make, like the backup restore (Gaia
still creates no database but the `demo_pg` fixture). Types and indexes change
on the way (`datetime2` → `timestamp`, no columnstore), and it costs you an hour
or two. Tell me whether you want it; I will then write the exact steps (cast
rules, the read-only role for its schemas, the target entry).

---

## 15 · Phase 4A: the SQL guard, the executor, query runs

What is new (PHASES, "Phase 4" → "4A results", D57): every SQL statement Gaia will
ever run for a question goes through a guard that accepts only a single read-only
query over objects in Gaia's catalog, with functions from an allowlist, joins backed
by a declared FK, a tested model hint (D48), an SME hint or view lineage, and a row
cap. What runs is the guard's rendering, in a read-only session with the statement
timeout; every run is recorded in `chat.query_run`. `make query` runs one statement
through all of it; `make query-check` only validates.

### 15a · Migrate and test (S1)

```bash
cd backend && ../gaia-env/bin/alembic upgrade head && cd ..
(make stamp; make test) > verification/15_tests.out 2>&1     # §9's exports, incl. GAIA_TEST_MSSQL=1
```

- [x] `Running upgrade 0006 -> 0007, Query runs (Phase 4A): …`
- [x] `gaia build 2026-11-16 · D57 (4A) · sqlglot <version>`
- [x] **575 passed, 0 skipped** (Ollama up; 573 passed, 2 skipped without). Sandbox: 565
      passed, 10 skipped (8 SQL Server live, 2 Ollama). The PostgreSQL and offline tests
      must pass (blocking); a failing SQL Server live test is S1 (non-blocking) — send it.
      Look for `test_guarded_statements_run_on_sql_server` and
      `test_executor_timeout_and_forced_write_on_sql_server`; `-s` prints what the first ran.

### 15b · On your PostgreSQL host (L2)

```bash
(make stamp; for f in infra/pg-target/queries/*.sql; do echo "== $f"; \
   make query TARGET=demo_pg ARGS="--file $f --rows 5"; done) > verification/15_query_pg.out 2>&1
```

- [x] `01_revenue_by_city`: `ACCEPTED`, `join : ok fk`, `row cap : 1000 (added)`, `execution : ok`
      *(D65, `15_query_pg.out`: 360 rows in 243 ms)*
- [x] `02_legacy_hint_join`: `ACCEPTED`, `join : ok hint legacy.tb_trx02.f2 = crm.customers.customer_code`
      — your model proposed that join and its test passed at 99 % (§11). In the sandbox it is
      rejected (no model ran there). If yours says `unverified_join`, the hint is no longer
      `inferred` (e.g. `stale`): send the line and the SQL below *(accepted with `ok hint`; 4
      segments, retail 796,459,393.75 — the g16 reference)*
- [x] `03_view_lineage_join`: `ACCEPTED` with `hint_via_view` (the view column *is* `f2`), same
      condition (`shared_key` on the D57 build; the join check was refined in D58) *(same 4
      totals through the view)*
- [x] `90_…`, `91_…`: `REJECTED` (`function_not_allowed`, `write_operation`), exit 2, nothing executed
- [x] every accepted and rejected run printed `recorded : chat.query_run …` *(5 of 5)*

```sql
-- make psql
SELECT status, origin, left(submitted_sql, 60), row_count, row_cap_reached, elapsed_ms,
       semantic_versions
FROM chat.query_run ORDER BY created_at DESC LIMIT 10;
SELECT from_columns, to_table, to_columns, status FROM semantics.join_hint
WHERE source = 'llm' AND from_table = 'tb_trx02';
```

### 15c · On SQL Server (S4 — asynchronous; a bypass would be safety)

```bash
(make stamp; for f in infra/sqlserver/queries/*.sql; do echo "== $f"; \
   make query TARGET=demo_mssql ARGS="--file $f --rows 5"; done) > verification/15_query_mssql.out 2>&1
```

*Not run yet (triage 2026-11-17) — S4; `scripts/owner_runs.sh quick` includes it.*

- [ ] `01_customers_by_year`: `ACCEPTED` with `join : ok fk` and `execution : ok` — or
      `REJECTED … unverified_join` if `transactions.customer_id` has no declared FK (then send
      the line; it is the guard doing its job, not a defect)
- [ ] `02_all_transactions`: `SELECT TOP 1000 …`, `execution : ok`, `row cap reached`
- [ ] `90_refused_exec`, `91_refused_system_catalog`: `REJECTED`, nothing executed
- [ ] **safety**: no statement in `90_`/`91_` reached `execution :`

### 15d · Decisions before 4C/4D (L3)

P4-1 (golden questions: I write ~20 for `demo_pg` with expected results, you add 5–10
from a short business description without looking at the schema; `nge_small` questions
from you), P4-2 (answer style), P4-4 (clarify vs guess), P4-8 (`app.default_target` →
`demo_pg`). P4-3 (row limit 1000) is what 4A uses now. The correct-result target (≥ 70 %)
stays a trial, as you said: its first numbers come with 4D.

---

## 16 · Phase 4B: retrieval

What is new (PHASES, "4B results", D58, D59): `make retrieve` turns a question into
the schema context the SQL generator (4C) will see — the objects, columns, meanings
with their certainty, readable values, and the joins allowed. `make retrieval-eval`
measures it over a question set. Both read only Gaia's store (and Ollama for the
question's embedding), never a target.

Also changed in the guard (D58): joins that multiply rows are refused (`fan_out`). If
you ran §15 on the D57 build, `03_view_lineage_join` said `shared_key`; it now says
`hint_via_view`. Nothing else in §15 changes.

### 16a · Tests

```bash
(make stamp; make test) > verification/16_tests.out 2>&1     # §9's exports, incl. GAIA_TEST_MSSQL=1
```

- [x] `gaia build 2026-11-16 · D59 (4B) · sqlglot <version>`
- [x] **598 passed, 0 skipped** (596 / 2 without Ollama). Sandbox: 588 passed, 10 skipped.

### 16b · Retrieval on `demo_pg` (L5)

Best after §14d (every object reasoned); useful already now (legacy, rpt, hr are).

*Not run yet (triage 2026-11-17). §14d is done now, so this is the full picture — and the
last Phase 4 acceptance number (the retrieval trial). ~10 minutes; it needs Ollama.*

```bash
(make stamp; make retrieve TARGET=demo_pg ARGS="--question 'Legacy transaction amounts by customer segment' --context") \
  > verification/16_retrieve_pg.out 2>&1
(make stamp; make retrieval-eval TARGET=demo_pg ARGS="--file infra/pg-target/retrieval_questions.csv --out verification/16_retrieval_pg.json"; \
 make retrieval-eval TARGET=demo_pg ARGS="--file infra/pg-target/retrieval_questions.csv --no-vectors") \
  > verification/16_retrieval_pg.out 2>&1
```

- [ ] `16_retrieve_pg.out`: no `embeddings unavailable` note (Ollama answered); the context
      holds `legacy.tb_trx02` and `crm.customers` joined by
      `legacy.tb_trx02.f2 = crm.customers.customer_code (tested, inferred by the model)`; the
      meanings of `f2`, `f7` are your model's, with `(uncertain …)` where not accepted
- [ ] `16_retrieval_pg.out`: two summary lines (with vectors, then without). The sandbox, with
      meanings derived from names and no vectors, gave `covered 100% · connected 86% ·
      recall@5 91% · MRR 0.92`; with your meanings the two legacy questions should become `ok`
      (the `f2`, `f9` hints). Whatever the numbers, send both files: they set the retrieval
      defaults (and the proposed retrieval target, PHASES Phase 4 acceptance)

### 16c · Retrieval on `nge_small` (S6)

*Not run yet (triage 2026-11-17) — S6. Since §13d every `nge_small` object has a meaning,
so the note under the box below no longer applies.*

`infra/sqlserver/retrieval_questions.csv` is a **draft** I wrote from the table names in
your outputs. Correct the `expected` objects (and add questions you care about — they can
seed the golden set, P4-1), then:

```bash
(make stamp; make retrieval-eval TARGET=demo_mssql ARGS="--file infra/sqlserver/retrieval_questions.csv --out verification/16_retrieval_mssql.json") \
  > verification/16_retrieval_mssql.out 2>&1
```

- [ ] it runs; most of `nge_small` has no meanings yet (only the 10 tables of the Phase 2
      scope are reasoned until §13d), so expect names-only retrieval elsewhere — the output
      says `(no meaning yet)` for those objects in `make retrieve … --context`

---

## 17 · Phase 4C: questions answered

What is new (PHASES, "4C results", D60): `make ask` answers a business question — a
table and a narrative (P4-2), or a clarifying question (P4-4), or "cannot answer" —
with what it computed, what it assumed, which meanings it relies on that are not
confirmed, and an explained confidence. The SQL is hidden unless `--show-sql`. These
are the first calls of the configured model for SQL: thinking is off for generation,
repair and the narrative (`llm.thinking.sql_generation / repair / answer`), so each call
should take seconds to tens of seconds, not minutes.

### 17a · Tests

```bash
(make stamp; make test) > verification/17_tests.out 2>&1     # §9's exports, incl. GAIA_TEST_MSSQL=1
```

- [x] `gaia build 2026-11-16 · D60 (4C) · sqlglot <version>`
- [x] **614 passed, 0 skipped** (612 / 2 without Ollama). Sandbox: 604 passed, 10 skipped.

### 17b · Five golden questions on `demo_pg` (L6)

From `infra/pg-target/golden.yaml`; `--as-of 2026-01-15` because the data ends in 2025.

```bash
(make stamp
 for q in "What was the total order amount in December 2025, excluding cancelled orders?" \
          "Which store city had the highest order amount last month, excluding cancelled orders?" \
          "What is the total amount of overdue invoices per currency?" \
          "What is the total amount of the legacy transactions per customer segment?" \
          "Who are our best customers?"; do
   echo "=== $q"; make ask TARGET=demo_pg ARGS="--question \"$q\" --as-of 2026-01-15 --show-sql"
 done
 echo "=== follow-up"
 make ask TARGET=demo_pg ARGS="--question 'Who are our best customers?' --clarified 'the ten customers with the highest order amount in 2025, cancelled orders excluded' --as-of 2026-01-15 --show-sql"
) > verification/17_ask_pg.out 2>&1
```

The references (what the sandbox's reference SQL returns on `demo_pg` at scale 1):

| Question | Reference |
|---|---|
| g01 December 2025, cancelled excluded | 51,222,131.78 |
| g02 best store city last month | Adana, 3,934,224.77 |
| g07 overdue invoices per currency | 3 currencies; TRY 69,329,887.03, EUR 754,758.42, … (status code 2 — hard: no catalog says "2 = overdue") |
| g16 legacy amount per segment | 4 segments; retail 796,459,393.75, smb 120,272,101.53, … (needs the `f2` hint) |
| c01 best customers | a clarification, not a number |

- [x] each answered question prints a narrative, the table, `confidence`, `computed` and, where
      relevant, `assumption` / `unconfirmed` lines; `model : N call(s)` with N = 2 (no repair)
      or 3–4 (repairs) *(D65, `17_ask_pg.out`: all four with 2 calls, no repair, 2–4 s each)*
- [x] the numbers: compare with the table above and note each as right / wrong / clarified
      — this is a first look, 4D does it for all 25 questions automatically
      *(g01 **right** 51,222,131.78 · g02 **right** Adana 3,934,224.77 · g16 **right**, all four
      segments · g07 **wrong**: "overdue" read as `due_on < 2026-01-15` over every status —
      1.46 bn TRY instead of 69.3 M — and the narrative says openly that paid and void invoices
      are included. Confidence 0.71 "medium" on the wrong answer, 0.20 "low" on g01 — apparently
      because this ran while §14d was still writing: at 19:20 `sales.orders` had no meaning yet
      and `finance.invoices` had one; by §19b (19:35) both had (§21 T7))*
- [ ] `Who are our best customers?` ends with `clarification : …` and options (exit 4); the
      follow-up answers with ten customers *(first half met. The follow-up with `--clarified
      '…'` (no id) **asked again**, and its option 1 was your text word for word — §21 T6. The
      same question answered correctly in the golden run, where the clarification's question
      travels with your answer)*
- [x] send the file, and this, which shows every attempt and what went wrong *(`17b_sql_result.out`:
      every run `ok` on attempt 1; the two refusals are §15b's)*:

```sql
-- make psql
SELECT created_at, attempt, status, left(question, 50), row_count, elapsed_ms, llm_ms,
       prompt_tokens, completion_tokens, left(error, 120)
FROM chat.query_run WHERE origin = 'cli' ORDER BY created_at DESC LIMIT 20;
```

### 17c · Two questions on `nge_small` (S7)

```bash
(make stamp
 make ask TARGET=demo_mssql ARGS="--question 'What was the total transaction amount per currency in 2024?' --show-sql"
 make ask TARGET=demo_mssql ARGS="--question 'How many customers made at least one transaction?' --show-sql"
) > verification/17_ask_mssql.out 2>&1
```

- [x] answers or clarifications — not `failed`; the SQL is T-SQL (`TOP`, `YEAR()`/`DATEADD`), and
      any repair says why in `--show-sql`'s `problem:` lines. Send the file. *(D65,
      `17_ask_mssql.out`: both answered on attempt 1 with `SELECT TOP 1000 …`. "How many
      customers …" → 290,486. "… per currency in 2024" → **no rows**, and the narrative says the
      period is the likely reason. Whether `nge_small` really has no 2024 transactions is §21 Q2)*

---

## 18 · Phase 4D: the golden run

What is new (PHASES, "4D results", D61–D63): `make golden` runs a golden set through the
whole pipeline and scores every final answer against its reference by its *result* (not
its SQL). Your seven questions are in `infra/pg-target/golden.yaml` as o01–o07. All seven
are ambiguous in a way that changes the numbers, so each expects a clarification; the
`clarified` line of each is the answer I assumed you would give. **Please read those seven
and correct any `clarified` text that is not what you meant** — the reference SQL follows it,
and I will rewrite the SQL to match.

Gaia now also records every clarification with its best guess (D61): the option it would
have chosen and that option's query (checked, never run for you). The runner scores that
guess against your clarified answer — the first measurement of whether Gaia could one day
answer such questions without asking.

### 18a · Migrate and test

```bash
cd backend && ../gaia-env/bin/alembic upgrade head && cd ..      # 0007 -> 0008
(make stamp; make test) > verification/18_tests.out 2>&1         # §9's exports, incl. GAIA_TEST_MSSQL=1
```

- [x] `Running upgrade 0007 -> 0008, Clarifications (D61): …`
- [x] `gaia build 2026-11-16 · D63 (4D) · sqlglot <version>`
- [x] **626 passed, 0 skipped** (624 / 2 without Ollama). Sandbox: 616 passed, 10 skipped.

### 18b · The golden run of `demo_pg` (L7)

About 80 model calls (generation, clarifications, re-asks after them, narratives; repairs
on top), thinking off, three at a time: expect 30–60 minutes. Resumable: if it stops, run
the same command again; answered calls are replayed. Best after §14d (all `demo_pg`
objects reasoned); useful before it too.

```bash
(make stamp; make golden TARGET=demo_pg ARGS="--file infra/pg-target/golden.yaml --out evaluations/golden_demo_pg") \
  > verification/18_golden_pg.out 2>&1
```

- [x] it ends with the report (also in `evaluations/golden_demo_pg/report.md`): the two
      acceptance lines (executable ≥ 80 %, correct ≥ 70 %), the clarification lines, cost, and a
      table of all 32 questions with why each wrong one is wrong *(D65, `18_golden_pg.out`:
      **executable 100 %, correct 97 % (29 of 30)**; 72 calls, 213,525 tokens, 2 repairs,
      **120 s** for the whole set)*
- [ ] the same command once more prints `model calls: 0 new` *(not run — optional)*
- [x] **send** `report.md`, `results.json` and `18_golden_pg.out` — with them I read every wrong
      answer, fix what is Gaia's fault (prompt, retrieval, guard) and tell you what is the
      model's; that is the Phase 4 gate's evidence *(the report is complete in the `.out` file.
      What it lacks is the SQL of the one wrong answer, g07; §17b shows the model's reading of
      the same question, and the report's reason is consistent with it — §21d's summary is
      enough, `results.json` itself is not needed)*

*Triage (2026-11-17).* The one wrong answer, **g07** (overdue invoices), is the question the set
marks "hard": overdue is `status = 2`, and nothing in any catalog says so. In §17b the model
read the same question as "due date before today, every status" and stated that assumption in
its narrative; the golden report's reason (no column holds the reference's values) fits it.
That is the model's limit, not Gaia's, and it is exactly what an SME review settles: one
confirmed value meaning (`finance.invoices.status`: 2 = overdue). Phase 5 makes this its
acceptance test. The other lines:

* answer questions: 20 of 21 right the first time, **0 over-clarified**;
* clarify questions: 10 of 11 asked back (c04 `cannot_answer` counts as right). **o05** ("most
  returned products") was answered without asking — a policy miss — and its number happened to
  be the one you meant, so it counts as correct (28 of 29 without it);
* best guess = your clarified answer in **2 of 8** (o01, o03). That is the D61 measurement, and it
  says that for these questions Gaia should keep asking. **In all nine clarifications of this run
  Gaia's own choice was option 1** (13 of 13 across §17b, §18b, §18c and §19b), so the order the
  options are shown in gives Gaia's choice away — §21 T5, to be fixed before real users answer
  clarifications.

### 18c · A clarification of yours (L8)

```bash
(make stamp
 make ask TARGET=demo_pg ARGS="--question 'Top 5 shops performing best last year.' --as-of 2026-01-15"
) > verification/18_clarify.out 2>&1
# then answer it yourself, with the id it printed: a number, or your own words
make ask TARGET=demo_pg ARGS="--clarification <id> --clarified <your answer> --as-of 2026-01-15" \
  >> verification/18_clarify.out 2>&1
make clarifications TARGET=demo_pg >> verification/18_clarify.out 2>&1
```

- [x] the first command prints a question and 2–4 numbered options, and the id (exit 4)
      *(D65, `18_clarify.out`: 4 options, exit 4)*
- [ ] the second answers; if you chose the option Gaia would have chosen, it says `answered
      with the best guess stored with the clarification` (no generation call) *(its output is
      not in the file — it was probably not redirected. The same path is proven in §19b:
      `answered with the best guess stored with the clarification`, 1 call — the narrative)*
- [x] `make clarifications` lists it with `agrees` or `differs` *(`measure 1/1 (100%)`, `agrees`;
      Gaia's guess was option 1, and so were the two `best customers` clarifications — §21 T5)*

### 18d · `nge_small` (S8, when its questions exist)

When you have questions for `nge_small` (P4-1), send them; I write `infra/sqlserver/golden.yaml`
and the run is the same command with `TARGET=demo_mssql`.

---

## 19 · Phase 4E: the chat API

What is new (PHASES, "4E results", D64): conversations over HTTP. A question streams its
progress as server-sent events — the table arrives before the narrative — and the answer, a
clarification, "cannot answer" or a failure is stored as a turn. Answering an open
clarification is just the next message ("1" picks the first option). A follow-up ("and in
2024?") is read with the conversation so far. No login yet (P5-1): the API listens on
127.0.0.1 only.

### 19a · Migrate and test

```bash
cd backend && ../gaia-env/bin/alembic upgrade head && cd ..      # 0008 -> 0009
(make stamp; make test) > verification/19_tests.out 2>&1
```

- [x] `Running upgrade 0008 -> 0009, Conversations (Phase 4E): …`
- [x] `gaia build 2026-11-16 · D64 (4E) · …`; **629 passed, 0 skipped** (sandbox: 619 + 10 skipped)

### 19b · A conversation (L9)

In one terminal: `make dev-api` (uvicorn on 127.0.0.1:8000). In another:

```bash
API=127.0.0.1:8000/api
say() { curl -sN -X POST "$API/conversations/$CID/messages" -H 'content-type: application/json' \
          -d "{\"content\": \"$1\", \"as_of\": \"2026-01-15\"}"; echo; }
(make stamp
 curl -s $API/data-sources; echo
 CID=$(curl -s -X POST $API/conversations -H 'content-type: application/json' \
        -d '{"data_source": "demo_pg"}' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
 say "Which three store cities had the highest order amount in December 2025, excluding cancelled orders?"
 say "And in November 2025?"
 say "Top 5 shops performing best last year."
 say "1"
 curl -s $API/conversations/$CID | python3 -m json.tool
) > verification/19_chat.out 2>&1
```

- [x] each `say` prints `event: message`, several `event: status`, `event: result` (before the
      narrative), then `event: answer` (or `event: clarification`), `event: done`; a
      `: keep-alive` line appears while the model works more than 15 s *(D65, `19_chat.out`: in
      that order, every turn. No keep-alive — no turn took more than 3.3 s, so none was due)*
- [x] the follow-up's answer is about November 2025 and the same measure *(top three cities,
      order amount, cancelled excluded: Adana, Antalya, Gaziantep)*
- [x] "Top 5 shops…" ends with `event: clarification` and options; `1` answers it *(from the
      stored best guess: `answered with the best guess stored with the clarification`, 1 call —
      the narrative)*
- [x] the last command lists the eight turns; the stored answers carry no SQL *(8 turns; no SQL
      in any stored payload)*
- [x] send `19_chat.out`

*Triage note for Phase 5 (2026-11-17): the `clarification` event sends the client
`clarification.default` and the `best_guess` (its interpretation and assumptions). The CLI
never prints them, but a browser would receive Gaia's choice, which D61 keeps from the user —
§21 T5.*

---

## 20 · Phase 4F: data sources added at runtime

What is new (PHASES, "4F results", D65): a data source can be added without editing
`config/gaia.yaml` — `make datasource ARGS="add …"` — with its password encrypted in the
metadata store (AES-256-GCM, the key in `.env`, never in the database). It is unusable until
the read-only probe passes (`verified`, the same 9 checks as `make check-target`), and again
after any change or after `datasources.reprobe_days` (30). Then every command works with it:
`TARGET=<name>`. Config targets are unchanged. Also new: `make evaluate-freeze` and
`GAIA_LLM_CONCURRENCY` (the run plan above), `scripts/owner_runs.sh`.

### 20a · Setup (once)

```bash
make master-key >> .env                  # GAIA_MASTER_KEY=…  — keep it; losing it loses stored passwords
scripts/owner_runs.sh setup              # make venv (new: cryptography), alembic upgrade head (→ 0010), make test
```

- [x] `20_migrate.out`: `… -> 0010, Data sources added at runtime (Phase 4F) …` *(the file shows
      no upgrade line: the store was already at head. 0010 is applied — §19b's `GET
      /api/data-sources` reads its columns and succeeded)*
- [ ] `20_tests.out`: `gaia build 2026-11-16 · D65 (4F) · …` and **636 passed, 0 skipped** (sandbox: 626 +
      10 skipped) *(`20_tests.out`: **591 passed, 45 skipped** — the §9 exports were not set in
      that shell, so every database test skipped. The full run is `all_test_latest.out` (no
      stamp): **634 passed, 2 failed, 0 skipped** — the two failures are 4F tests that assumed
      the sandbox's trust authentication; §21 T1 has the one-file re-run)*

### 20b · A data source of your own (L10)

The `demo_pg` database once more, added at runtime under another name:

```bash
(make stamp
 read -rs -p "gaia_ro password: " PW; echo; export PW
 make datasource ARGS="add --name pg_copy --dialect postgres --host 127.0.0.1 --port 5434 --database gaia_target_pg --user gaia_ro --password-env PW"
 make introspect TARGET=pg_copy                   # refused: not verified yet
 make datasource ARGS="probe pg_copy"
 make introspect TARGET=pg_copy
 make query TARGET=pg_copy ARGS="--sql 'SELECT count(*) AS n FROM sales.orders' --no-record"
 make datasource ARGS="list"
 make datasource ARGS="remove pg_copy --yes"
) > verification/20_datasource.out 2>&1
```

*Not run yet (triage 2026-11-17) — L10.*

- [ ] the first `introspect` is refused: `data source 'pg_copy' is unverified …`
- [ ] `probe` prints `verified` and nine `[PASS]` lines
- [ ] the second `introspect` reads 28 objects; the query returns 250,000
- [ ] `list` shows `pg_copy admin verified`, and never a password; `remove` keeps it as
      `pg_copy~…` (tombstoned)
- [ ] optional, a safety check: the same `add` with `--user postgres` (the container's superuser)
      must end `failed` at `probe`, with `security.privileges` failing

### 20c · The tracks

`scripts/owner_runs.sh quick`, `… gateway`, `… gateway-long` — when and as you can (the run
plan, top of this file). Send `verification/20_*.out` as they appear.

*Triage note (2026-11-17): only `setup` ran as a track (`20_venv`, `20_migrate`, `20_tests`); the
rest was run by section (§14–§19), which works just as well. What is still open, §21c lists.*

---

## 21 · Triage of the D65 outputs (2026-11-17)

I read every file in `verification/` that was new since §14: the ones stamped
`2026-11-16 · D65 (4F)` (git `2c06491`), plus `13_reason_all.out`, `13b_evaluate_*.out` and
`all_test_latest.out`, which have no stamp. The boxes above are ticked where a file proves
them, and each note says which file. **No safety finding** (D52 b): every refusal refused, and
nothing that should have been unusable became usable.

### 21a · Confirmed on your host

| What | Where | Result |
|---|---|---|
| **Phase 4 acceptance: the golden run** | §18b | executable **100 %**, correct **97 %** (29/30); 0 of 21 clear questions over-clarified; 10 of 11 ambiguous ones asked back |
| 4A guard and executor on PostgreSQL 18 | §15b | all five as expected; the D48 hint join and its view-lineage twin give the g16 numbers |
| The chat API | §19b | streaming order, follow-up, clarification answered from the stored guess with one call, 8 turns, no SQL stored |
| D54 neighbour control on SQL Server (S2) | §14c | the four counts are no longer kept; four `session_id → page_view_id` false positives went too; date keys kept |
| `dw.sales_wide` (S3) | §14c | `profiled` (was `partial` since 3A) |
| Production reasoning of all of `nge_small` (§13d) | `13_reason_all.out` | 71 objects, `failed=0`, 159 calls, 55 min; D53 chunking rescued six tables |
| `demo_pg` semantics (L1) | §14d | 28 objects, `failed=0` |
| 3C: N-C and N-P | `13b_*` | complete, 99 columns each |
| 3C inputs frozen (D66) | `14_BC_BP.out` | B-C and B-P replayed from `inputs.json`, **0 new calls** — the freeze reproduces the prompts exactly |
| T-SQL answers (S7) | §17c | both answered with `TOP`, none failed |
| SQL Server live tests (S1) | `all_test_latest.out` | all passed (0 skipped) |

**The model changed, and it is ~12× faster.** Up to §12 the outputs say `deepseek-v4.1-flash`;
§13d, §14 and §18 say `deepseek-flash` (so does `.env.example`). With thinking on, §13d produced
1.05 M output tokens in 5,536 s of call time, **~190 tokens/s** against the ~15 tokens/s every
estimate in PHASES was built on: all 71 objects in 55 minutes, not "overnight". The golden run
took 2 minutes. PHASES and README are updated; the 3C consequence is T3.

### 21b · Findings

| # | Finding | Why it matters | Next |
|---|---|---|---|
| T1 | **Two 4F tests failed** (`all_test_latest.out`): a superuser data source failed the probe on `connect` instead of `security.privileges`, and the admin API's probe ended `failed`. Both tests used placeholder passwords that only the sandbox (which trusts any password) accepts | The product did the right thing: neither data source became usable. But the tests did not prove on your host what they prove in the sandbox. The test file delivered in this build **already** reads your real passwords (`GAIA_TEST_PG_TARGET_URL`, `GAIA_PG_TARGET_ADMIN_PASSWORD`), and the run used an older copy. Because the build marker stayed `D65`, the stamp cannot tell the two copies apart (D56) | You: re-run that file (§21c-1). Me: bump the build marker with every change to the code or the tests, even test-only ones |
| T2 | **The second `make reason` of §14d re-reasoned 24 tables (46 calls)** although Stage 3 had nothing to test. It had only 8 cache hits, and it printed exactly the first run's score distribution although its own table means work out to 0.764, not 0.768 | Both facts fit best with the two commands **running at the same time**: each planned from the same state, and the last writer of each table won. Reasoning has no per-data-source lock, whereas profiling has one (D33). Nothing is corrupt — every version is a complete answer — but the cost doubled, and an admin screen that can start runs (Phase 5) must not allow this | You: Q1. Me (Phase 5, step 5.0): one reasoning/re-scoring run per data source at a time, refused with a clear message like profiling's |
| T3 | **The model changed in the middle of 3C.** The recorder keys answers by the request and not by the model (that is what makes resuming possible), so a condition answered partly before and partly after the switch mixes two models, and nothing reports it | Criteria 1 and 2 compare conditions (B-P with B-C, N-P with N-C); criterion 3 reads B-P alone. B-C and B-P were answered on 2026-11-15/16 (B-P's last 4 answers on the 16th), and N-C/N-P probably later. If the switch fell between two compared conditions, or inside B-P, the comparison is between models, not between kinds of evidence | You: Q3, and the model count in §21d (no data in it). Me (5.0): the 3C report names the model(s) behind every condition and marks a mixed one |
| T4 | **B-P-nt: 7 calls refused with `http 400`.** The harness kept only `http 400`, not the gateway's reason, although the reason reaches the call record | It breaks "failures recorded, not swallowed" for evaluation runs, and the cause cannot be found from here. Thinking-off calls with the same `max_tokens` succeeded on the same model in §17–§19, with prompts about as large (~5.5–6 k tokens, `payments`' was 5.9 k) — so neither the parameter nor the size explains it, and I will not guess | Me (5.0): keep the first 500 characters of the gateway's answer for a failed call in `results.json`. You: run B-P-nt again after that build, not before |
| T5 | **Gaia's choice is always option 1** (13 of 13 clarifications), and the API sends `clarification.default` and `best_guess` to the client | D61 measures how often the user picks Gaia's option, and keeps Gaia's choice hidden so the measurement is not pulled toward it. Showing it first pulls exactly that way (people favour the first option), and a browser would receive the choice outright. The golden measurement is unaffected (it compares your clarified text, not a position) | Me (5.0, before the chat screen): show the options in an order shuffled per clarification and stored with it; keep `default`/`best_guess` on the server |
| T6 | `make ask --question … --clarified '…'` (no id) **asked again** (§17b); with the id — golden, API, `--clarification ID` — it answers | A CLI edge: the answer travels without the question it answers, and the prompt has no rule "a clarification settles what it answered" | Me (5.0): both, small |
| T7 | Confidence is not correctness yet: g07 (wrong) 0.71 "medium"; g01 (right) 0.20 "low" — the latter apparently because §17b ran while §14d was still writing (see §17b) | Expected until Phase 6 calibrates; the UI must not present it as a probability | Phase 5 shows the level and its reasons, never a percentage |
| T8 | Outputs without a stamp (`all_test_latest.out`, `13_reason_all.out`, `13b_*`); `20_tests.out` ran without the §9 exports (45 skipped) | A result that cannot be tied to a build cannot be triaged against it (D56) | You: `(make stamp; …)` as in the commands, and the §9 exports in the shell that runs `setup` |

### 21c · What to run next (~30 minutes; no LLM gateway)

```bash
# the §9 exports first (GAIA_TEST_DATABASE_URL, GAIA_TEST_PG_TARGET_URL, GAIA_TEST_MSSQL=1);
# .env must hold GAIA_PG_TARGET_ADMIN_PASSWORD (§1)
# 1. T1 — the 4F tests with your real passwords
(make stamp; make test ARGS="tests/integration/test_datasources_pg.py -v") > verification/21_datasource_tests.out 2>&1
# 2. the retrieval trial — the last Phase 4 acceptance number (§16b, L5)
#    run the two command blocks of §16b as written
# 3. SQL Server, asynchronous: §15c (S4) and §16c (S6) as written
# 4. L10 by hand: §20b
# 5. Q2, one read-only statement through the guard
(make stamp; make query TARGET=demo_mssql ARGS="--sql 'SELECT MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at FROM dbo.transactions' --no-record") \
  > verification/21_q2_mssql.out 2>&1
```

- [ ] `21_datasource_tests.out`: **2 passed**. If the superuser test still fails on `connect`,
      `GAIA_PG_TARGET_ADMIN_PASSWORD` in `.env` is not the container's superuser password
- [ ] §16b's two files — with them the Phase 4 retrieval trial is decided
- [ ] §15c: nothing in `90_`/`91_` reaches `execution :` (safety)

Not now: B-P-nt (after T4's build), and judging/reporting 3C (after Q3 and the model count —
judging a condition that mixes models would measure the wrong thing).

### 21d · What to send from `evaluations/` — no data values

Not the directory itself: these two summaries are all I need, and they contain only table names,
model names, counts and error texts.

```bash
python3 - <<'EOF' > verification/21_evaluations_summary.out 2>&1
import collections, datetime, json, pathlib
for run in ("evaluations/nge_small", "evaluations/golden_demo_pg"):
    print("==", run)
    c = collections.Counter()
    for f in pathlib.Path(run, "llm").glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        day = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:00")
        c[(day, d.get("purpose", "?"), d.get("model", "?"), d.get("finish_reason"))] += 1
    for key, n in sorted(c.items()):
        print("  ", *key, n)
    for r in sorted(pathlib.Path(run).glob("*/results.json")):
        s = json.loads(r.read_text(encoding="utf-8")).get("summary", {})
        errors = collections.Counter(x.get("error") for x in s.get("calls", []) if x.get("error"))
        print("  ", r.parent.name, "columns", s.get("columns"), "failed", s.get("failed"),
              "critique_failed", s.get("critique_failed"), "errors", dict(errors))
EOF
```

### 21e · Three questions

* **Q1** — In §14d, did the second command (`profile-tests; reason`) start before the first
  `make reason` had finished — another terminal, or pasted while it ran? (T2: if not, the
  re-reasoning is a defect I have to reproduce, not a missing lock.)
* **Q2** — Does `nge_small` hold transactions in 2024? §21c-5 answers it. If it does, the §17c
  answer "no rows" is wrong and goes to triage.
* **Q3** — Is `deepseek-flash` the former `deepseek-v4.1-flash` under a new id, or a different
  model? When did you switch? (T3 and the documentation.)

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
