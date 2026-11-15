# Phase 3B replay smoke-test fixtures

A functional end-to-end test of prompt generation 2 on the PostgreSQL development
target, with a person acting as the model through the replay provider
(`gaia.llm.replay`).

| File | What it is |
|---|---|
| `evidence.json` | Evidence for `legacy.tb_trx02`, `legacy.tb_mst01`, `hr.departments`, in the `gaia.profiling.summary` format. Computed from `demo_pg` at the default seed (scale 1, seed 42) by a throw-away script standing in for Phase 3A's profiler: exact counts, distinct counts, top values, samples, simple detectors, the identity `f7 = f5 × f6` and the view aliases of `rpt.v_legacy_transactions`. |
| `responses/*.json` | Six model answers (three reasoning, three critique), keyed by request hash. |

## Integrity note — read before using any number from this

The answers were written by the assistant that **also designed the `demo_pg`
database**, so it knew what every column means. This run therefore proves that
the pipeline works end to end with real-model-quality answers — prompts render,
answers validate, evidence refs resolve, the ledger scores, the queue ranks —
and it found five defects that were fixed (PHASES, "3B results"). It is **not**
a measurement of semantic accuracy, of any provider, or of the worst case. Every
stored row says `model = replay`. Accuracy is measured in 3C with the configured
model and a name-blind simulation.

## Reproduce

```bash
make pg-target-up && make pg-target-seed && make introspect TARGET=demo_pg
cp -r tests/fixtures/replay_3b /tmp/replay_3b
make reason TARGET=demo_pg ARGS="--replay /tmp/replay_3b --evidence-file /tmp/replay_3b/evidence.json \
  --table legacy.tb_trx02 --table legacy.tb_mst01 --table hr.departments --no-embed"
make review TARGET=demo_pg
```

Expected: `reasoned=3 failed=0`, `calls=6`, bands `accepted 10, deep_profile 8,
deep_profile_flagged 2, abstain 1`, 11 review items led by
`legacy.tb_mst01.c07` and `legacy.tb_trx02.f2`.

The first smoke run (the one that found the defects) used scale 10; these
fixtures were regenerated at scale 1 because the row estimate is part of every
prompt, and with it of every request key.

**Re-keyed 2026-11-14 (Phase 3A).** The v2 prompts gained explanations of the
3A evidence fields (`granularity`, `lineage`, alias `via`, test origins), which
changed every request key. The six answers were moved to the new keys
unchanged — this evidence file contains none of those fields, so the answers
are still answers to what the prompts show — and the expected result below was
re-verified on a fresh metadata database.

**Re-keyed 2026-11-15 (3A fixes).** The object list in every generation-2
reasoning prompt now names alternate (unique) keys as well as primary keys, so
the three reasoning requests have new keys; the critique requests are
unchanged. None of the three answers proposes a join, so they are still
answers to what the prompts show; the expected result is unchanged and was
re-verified.

Request keys are hashes of the exact prompts. If the run reports requests
*awaiting an answer*, your prompts differ from the sandbox's — most likely
because PostgreSQL 18 renders a type or default differently from 16 in the
snapshot. That is worth knowing; the answers in `responses/` can still be copied
to the new keys by hand (same table and purpose).
