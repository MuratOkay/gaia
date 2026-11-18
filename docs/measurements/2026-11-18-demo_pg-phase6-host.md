# Measurement note — Phase 6 on the owner's host (demo_pg, 2026-11-17/18)

What the evaluation store measured on the owner's host while Phase 6 was built (OWNER_VERIFICATION
§28–§32). Host: PostgreSQL 18 metadata store and `demo_pg` target (scale 1, seed 42, 2,973,244
rows), Ollama `bge-m3`, model `deepseek-flash` through the gateway; golden set
`infra/pg-target/golden.yaml` (32 questions, 30 with a reference; question-set key
`e469c7356b163f94`), "today" 2026-01-15. Every run fresh (no replay), operator's policy.

## Golden runs, per build

| Build | Runs (correct of 30) | Pooled | 95 % interval | Executable | Per run |
|---|---|---|---|---|---|
| D80 | 29, 28, 29, 29 (four runs; the report pooled three) | 86 / 90 = 96 % | 89–98 % | 100 % | — |
| D83 | 28, 29, 29 | 86 / 90 = 96 % | 89–98 % | 100 % | 73 calls, ~211 k tokens |
| D88 | 28, 26, 27 | 81 / 90 = 90 % | 82–95 % | 100 % | 71 calls, ~204 k tokens |
| D88 + regression `joins-rule` | 27, 28, 27 | 82 / 90 = 91 % | — | 100 % | 73 calls, ~208 k tokens |
| D90 (SQL prompt v4) | 27, 27, 28 | 82 / 90 = 91 % | — | 100 % | 73 calls, ~211 k tokens |
| D90 + regression `drop:store.stores` | 26, 24, 24 | 74 / 90 = 82 % | — | — | 71 calls, ~202 k tokens |
| D90 + regression `drop:finance.invoices` | 26, 25, 27 | 78 / 90 = 87 % | — | — | 72 calls, ~214 k tokens |

A run takes about two minutes (3.3 s median per question, 7–8 s at the 95th percentile).
Flaky across D88–D90: g03, g05, o02, o03, o05; wrong in every run until D90: o06 (retrieval —
the context lacked `sales.orders`; D91 addresses it).

## Comparisons

| Baseline → candidate | Difference (points) | Interval | Named breaks | Verdict | Note |
|---|---|---|---|---|---|
| D80 → D83 | −0.3 | −3.3 to +2.5 | — | cannot tell | old method (questions only) |
| D83 → D88 | −5.6 | −11.1 to −1.1 (old) · **−14.4 to 0.0** (P6-8) | none | cannot tell | no prompt changed; 4 questions moved, all down: sign test p = 0.125, whole-run permutation p = 0.20; 7.8 h apart |
| D88 → D88 + `joins-rule` | +1.1 | −2.2 to +5.6 (old, "passes") · −5.6 to +8.9 (P6-8) | none | cannot tell | repairs 1.0 → 1.0: the guard refuses what the rule forbids |
| D88 → D90 | +1.1 | −6.7 to +8.9 | none | cannot tell | the SQL prompt v4 (the chasm trap named) |
| D90 → D90 + `drop:store.stores` | −8.9 | −25.6 to +6.7 | **c03, g02, g17** (fixed: o06) | **fails** | expected c03, g02, g09, g17, o03 — 3 of 5 named: o03 flaky in the baseline (2/3), g09 answered from `store.inventory` alone |
| D90 → D90 + `drop:finance.invoices` | −4.4 | −16.7 to +6.7 | **g07** | **fails** | expected g07 — 1 of 1 |

The P6-8 interval resamples runs as well as questions. Simulated on these runs' flakiness (3 runs
a side, 150 trials each): a question that breaks in every run is named 100 % of the time, from one
broken question up; an unchanged build is failed 2.7 % of the time and *passes* 11 % (otherwise
*cannot tell*). The questions-only bootstrap needed ~6 broken questions (20 points) to *fail*
reliably and passed an unchanged build ~40 % of the time.

## The confidence shown, against the reference

| Runs | High | Medium | Low |
|---|---|---|---|
| D88 (3 runs) | 8 / 8 (68–100 %) | 65 / 72 = 90 % (81–95 %) | 8 / 10 = 80 % (49–94 %) |
| D88 + `joins-rule` (6 runs) | 16 / 17 = 94 % (73–99 %) | 135 / 147 = 92 % (86–95 %) | 12 / 16 = 75 % (51–90 %) |

"High ≥ 95 %" (P6-2) cannot be shown: the lower bound is far below, and the levels are not fitted.

## Labels from people

| Source | Labels | Verdicts | Notes |
|---|---|---|---|
| SME decisions (`demo_pg`, akaya) | 13 | 12 right, 1 wrong | 5 blind (4 accepted), 8 shown (8 accepted); AUROC 0.17 — one wrong label, not a measurement |
| The owner's 3C audit (`demo_mssql`) | 94 (17 production, N-P) | 70 right, 6 partly, 18 wrong | AUROC **0.74**; score ≥ 0.80: 22 of 22 right; the LLM judge: 81 of 94 (86 %), **Cohen's κ 0.65**, more lenient on 10 of the 13 disagreements |

Calibration (P6-1, counted per subject; P6-9 pools `deepseek-v4.1-flash`,
`deepseek/deepseek-v4.1-flash` into `deepseek-flash`): meanings 30 labels, 1 not right — too few
(75 / 15 needed for Platt); answers 180 labels on 30 questions, 6 not always right — too few.
Identity: meanings ECE 0.294, Brier 0.144; answers ECE 0.194, Brier 0.122.

## Category proposals, against people (6G)

| Data source | Confirmed | Proposed (one hidden at a time) | As confirmed | Proposals seen, kept |
|---|---|---|---|---|
| `demo_pg` | 28 | 28 | 26 (93 %) — direct 23/25, view 3/3 | none seen |
| `demo_mssql` | 30 | 29 | 18 (62 %) — direct 8/16, related 10/13 | 0 of 13 |

The misses cluster: *payment* counts for Sales and *inventory* for Store in today's categories,
where the owner says Finance and Product.
