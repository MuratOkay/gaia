"""Run evaluation conditions (PHASES 3C, "Evaluation design — simulating the worst case").

A condition fixes what the model may see:

| code     | names   | evidence                     | notes |
|----------|---------|------------------------------|-------|
| N-C      | visible | catalog only                 | the Phase 2 situation, with today's prompts |
| N-P      | visible | full Phase 3                 | production mode |
| B-C      | hidden  | catalog only                 | the floor |
| B-P      | hidden  | full Phase 3                 | **the worst case the product is built for** |
| B-P-nt   | hidden  | full Phase 3, thinking off   | proxy for a weaker or local model |
| N-P-raw  | visible | full Phase 3, raw values     | what masking costs (optional) |
| N-P-h    | visible | full Phase 3, ``--hide`` gone | e.g. demo_pg without the naming view |

"Full Phase 3" = Stage 1 evidence (profiles, detectors, identities, lineage,
join candidates) → one reasoning round → Stage 3 tests of *this condition's*
claims, in memory (``evaluation.stage3``) → a second round for the tables whose
evidence changed. Catalog-only conditions reason once with no evidence block.

Every condition runs the same prompts, scorer and critique as production, but
**writes nothing** to the metadata store: answers, scores and the tests it ran
go to the run directory, every completion to ``<run>/llm`` (resumable).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gaia.core.config import Settings
from gaia.evaluation.blind import Blinder, World, real_world
from gaia.evaluation.stage3 import ClaimSet, run_claim_tests, stage1_evidence, with_tests
from gaia.introspect.snapshot import Snapshot
from gaia.llm.base import LLMClient
from gaia.profiling.summary import StaticEvidence, TableEvidenceInput
from gaia.semantics.bundle import build_bundle, build_catalog_index
from gaia.semantics.evidence import (
    Exposure,
    build_ledger,
    column_coverage,
    detected_type,
    render_evidence,
)
from gaia.semantics.reasoner import ReasonerSettings, TableOutcome, TableReasoner
from gaia.semantics.scoring import score_column_v2
from gaia.semantics.service import declared_joins

__all__ = ["CONDITIONS", "DEFAULT_CONDITIONS", "Condition", "EvalContext", "run_condition"]


@dataclass(frozen=True)
class Condition:
    code: str
    names: str  # visible | blind
    evidence: str  # catalog | profiled
    description: str
    thinking: bool | None = None  # None: llm.thinking.semantics / critique
    raw: bool = False  # raw values to the model regardless of llm.locality
    hide: bool = False  # remove the run's --hide objects

    @property
    def profiled(self) -> bool:
        return self.evidence == "profiled"


CONDITIONS: dict[str, Condition] = {
    c.code: c
    for c in [
        Condition("N-C", "visible", "catalog", "names visible, catalog only"),
        Condition("N-P", "visible", "profiled", "names visible, full Phase 3 (production mode)"),
        Condition("B-C", "blind", "catalog", "names hidden, catalog only (the floor)"),
        Condition("B-P", "blind", "profiled",
                  "names hidden, full Phase 3 (the worst case the product is built for)"),
        Condition("B-P-nt", "blind", "profiled",
                  "names hidden, full Phase 3, thinking off (a weaker or local model)",
                  thinking=False),
        Condition("N-P-raw", "visible", "profiled",
                  "names visible, full Phase 3, raw values (the cost of masking)", raw=True),
        Condition("N-P-h", "visible", "profiled",
                  "names visible, full Phase 3, --hide objects removed", hide=True),
    ]
}
DEFAULT_CONDITIONS = ["N-C", "N-P", "B-C", "B-P", "B-P-nt"]


@dataclass
class EvalContext:
    settings: Settings
    data_source: str
    snapshot: Snapshot  # real, complete
    scope: list[str]  # real "schema.table", in reasoning order
    evidence: StaticEvidence  # real, as stored (filtered to Stage 1 per condition)
    llm: LLMClient
    out: Path
    hide: list[str] = field(default_factory=list)
    profiler_factory: Callable[[], Any] | None = None  # None: no Stage 3 (noted)
    seed: int = 42


def _hidden(snapshot: Snapshot, evidence: StaticEvidence, hide: list[str]
            ) -> tuple[Snapshot, StaticEvidence]:
    """The database without some objects: gone from the catalog, and no alias,
    lineage or test mentions them."""
    gone = {h.lower() for h in hide}
    tables = []
    for t in snapshot.tables:
        if t.qualified_name.lower() in gone:
            continue
        relations = [r for r in t.relations if f"{r.to_schema}.{r.to_table}".lower() not in gone]
        tables.append(replace(t, relations=relations))
    kept = {}
    for key, ev in evidence.tables.items():
        if key in gone:
            continue
        aliases = {c: [a for a in items if a.view.lower() not in gone]
                   for c, items in ev.aliases.items()}
        tests = {c: [t for t in items
                     if not (t.test_id.startswith("inc:")
                             and t.test_id[4:].rpartition(".")[0].lower() in gone)]
                 for c, items in ev.tests.items()}
        kept[key] = replace(ev, aliases={c: a for c, a in aliases.items() if a},
                            tests={c: t for c, t in tests.items() if t})
    return replace(snapshot, tables=tables), StaticEvidence(
        kept, profiling_available=evidence.profiling_available)


def _world(ctx: EvalContext, cond: Condition, snapshot: Snapshot,
           evidence: StaticEvidence, blinder: Blinder | None) -> World:
    if cond.names == "blind":
        assert blinder is not None
        return blinder.world(evidence)
    return real_world(snapshot, evidence)


def _reasoner(ctx: EvalContext, cond: Condition, world: World) -> TableReasoner:
    s = ctx.settings
    thinking = s.llm.thinking.semantics if cond.thinking is None else cond.thinking
    critique = s.llm.thinking.critique if cond.thinking is None else cond.thinking
    return TableReasoner(
        ctx.llm,
        ReasonerSettings(
            model=s.llm.model, thinking=thinking, critique_thinking=critique,
            max_tokens=s.llm.max_output_tokens, temperature=s.llm.temperature,
            validation_retries=s.semantics.validation_retries, prompt_major=2,
            context_budget_tokens=s.llm.context_budget_tokens,
        ),
        build_catalog_index(world.snapshot, unique_keys=True),
        declared_joins=declared_joins(world.snapshot),
    )


def _exposure(ctx: EvalContext, cond: Condition) -> Exposure:
    base = Exposure.from_settings(ctx.settings)
    return Exposure("raw", base.code_max_distinct) if cond.raw else base


def _reason(ctx: EvalContext, cond: Condition, world: World, tables: list[str]
            ) -> dict[str, tuple[TableOutcome, Any]]:
    """world table -> (outcome, rendered evidence). Tables, then views."""
    reasoner = _reasoner(ctx, cond, world)
    exposure = _exposure(ctx, cond)
    by_name = {t.qualified_name: t for t in world.snapshot.tables}

    def one(name: str) -> tuple[str, TableOutcome, Any]:
        table = by_name[name]
        bundle = build_bundle(table, dialect=world.snapshot.dialect)
        ev = world.evidence.table_evidence(name) if cond.profiled else None
        rendered = render_evidence(bundle, ev, exposure)
        return name, reasoner.run(bundle, rendered), rendered

    out: dict[str, tuple[TableOutcome, Any]] = {}
    workers = max(1, ctx.settings.llm.concurrency)
    for wave in ([n for n in tables if by_name[n].kind != "view"],
                 [n for n in tables if by_name[n].kind == "view"]):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, outcome, rendered in pool.map(one, wave):
                out[name] = (outcome, rendered)
    return out


def _claims(world: World, outcomes: dict[str, tuple[TableOutcome, Any]]) -> ClaimSet:
    claims: ClaimSet = {}
    for name, (outcome, _r) in outcomes.items():
        if outcome.reasoning is None:
            continue
        real = world.real_table(name)
        targets: dict[str, list[str]] = {}
        for h in outcome.join_hints:
            if len(h.from_columns) != 1 or len(h.to_columns) != 1:
                continue
            parent = f"{h.to_schema}.{h.to_table}"
            real_parent = world.real_table(parent)
            column = world.real_column(name, h.from_columns[0])
            targets.setdefault(column, []).append(
                f"{real_parent}.{world.real_column(parent, h.to_columns[0])}")
        for col in outcome.reasoning.columns:
            column = world.real_column(name, col.name)
            claims.setdefault(real, {})[column] = (col.role, targets.get(column, []))
    return claims


def _score(ctx: EvalContext, cond: Condition, world: World, name: str,
           outcome: TableOutcome, rendered: Any, round_no: int) -> list[dict[str, Any]]:
    s = ctx.settings
    bundle = outcome.bundle
    ev: TableEvidenceInput | None = (
        world.evidence.table_evidence(name) if cond.profiled else None)
    critiques = {c.name: c for c in (outcome.critique.columns if outcome.critique else [])}
    real_table = world.real_table(name)
    table_snapshot = next(t for t in ctx.snapshot.tables if t.qualified_name == real_table)
    types = {c.name: c.data_type for c in table_snapshot.columns}
    rows = []
    for col in outcome.reasoning.columns if outcome.reasoning else []:
        facts = bundle.facts[col.name]
        ledger = build_ledger(col, facts, bundle, ev, s.scoring.v2.strengths)
        crit = critiques.get(col.name)
        score = score_column_v2(
            col, facts, bundle, crit, s.scoring, s.profiling, ledger=ledger,
            evidence_refs=rendered.refs.get(col.name, set()),
            alias_names=[a.alias for a in ev.aliases.get(col.name, [])] if ev else None,
        )
        real_column = world.real_column(name, col.name)
        rows.append({
            "condition": cond.code,
            "table": real_table,
            "column": real_column,
            "world_table": name,
            "world_column": col.name,
            "data_type": types.get(real_column, facts.data_type),
            "business_name": col.business_name,
            "purpose": col.purpose,
            "role": col.role,
            "entity": col.entity,
            "unit": col.unit,
            "claimed_semantic_type": col.semantic_type,
            "semantic_type": detected_type(ev.columns.get(col.name)) if ev else None,
            "confidence": col.confidence,
            "critique": crit.verdict if crit else None,
            "score": score.calibrated_score,
            "band": score.band,
            "coverage": column_coverage(col.name, ev) if cond.profiled else "catalog",
            "round": round_no,
            "contradictions": sum(1 for i in ledger.contradicting if i.strength >= 0.5),
        })
    return rows


def _calls(outcomes) -> list[dict[str, Any]]:
    out = []
    for outcome, _r in outcomes:
        for c in outcome.calls:
            out.append({"purpose": c.purpose, "ok": c.parsed_ok, "cache": c.cache_hit,
                        "model": c.answered_by,
                        "prompt_tokens": c.usage.prompt_tokens,
                        "completion_tokens": c.usage.completion_tokens,
                        "reasoning_tokens": c.usage.reasoning_tokens,
                        "latency_ms": c.latency_ms}
                       | ({"error": c.error[:300]} if c.error else {})
                       | _gateway_answer(c))
    return out


def _gateway_answer(c) -> dict[str, Any]:
    """What the gateway said when it refused a call (5.0, T4): a ``400`` without its body
    cannot be diagnosed. Only transport failures — no model answer at all
    (``finish_reason`` is None) — so no model output or data value lands here."""
    if not c.error or c.finish_reason is not None or not c.content:
        return {}
    return {"gateway_response": str(c.content)[:500]}


# Reasoner notes worth a line in the summary (the rest — dropped join hints — are
# per-table detail already visible in the answers).
_SUMMARY_NOTES = ("truncated", "column chunks")


def run_condition(ctx: EvalContext, code: str) -> dict[str, Any]:
    """Reason, test (profiled), reason again where evidence changed, score; write
    ``<out>/<code>/results.json`` and return the summary."""
    cond = CONDITIONS[code]
    started = time.perf_counter()
    notes: list[str] = []
    snapshot, evidence = ctx.snapshot, stage1_evidence(ctx.evidence)
    if cond.hide:
        if not ctx.hide:
            raise ValueError(f"{code} needs --hide OBJECT")
        snapshot, evidence = _hidden(snapshot, evidence, ctx.hide)
    scope = [t for t in ctx.scope if snapshot.table(*t.split(".", 1)) is not None]
    blinder = Blinder(snapshot, seed=ctx.seed) if cond.names == "blind" else None
    world = _world(ctx, cond, snapshot, evidence, blinder)
    world_scope = [blinder.table(t) if blinder else t for t in scope]

    outcomes = _reason(ctx, cond, world, world_scope)
    all_calls = list(outcomes.values())
    tests_run: dict[str, Any] = {}
    second: list[str] = []
    if cond.profiled:
        if ctx.profiler_factory is None:
            notes.append("Stage 3 skipped: no target connection (--no-stage3)")
        else:
            results = run_claim_tests(
                ctx.settings, snapshot, evidence, _claims(world, outcomes),
                ctx.profiler_factory(), notes,
            )
            tests_run = {t: {c: [r.__dict__ for r in rs] for c, rs in cols.items()}
                         for t, cols in results.items()}
            if results:
                evidence = with_tests(evidence, results)
                world = _world(ctx, cond, snapshot, evidence, blinder)
                exposure = _exposure(ctx, cond)
                for name in world_scope:
                    table = next(t for t in world.snapshot.tables if t.qualified_name == name)
                    rendered = render_evidence(build_bundle(table, world.snapshot.dialect),
                                               world.evidence.table_evidence(name), exposure)
                    if rendered.hash != outcomes[name][1].hash:
                        second.append(name)
                if second:
                    again = _reason(ctx, cond, world, second)
                    all_calls += list(again.values())
                    outcomes.update(again)

    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    critique_failed: list[str] = []
    for name in world_scope:
        outcome, rendered = outcomes[name]
        real = world.real_table(name)
        notes.extend(f"{real}: {n}" for n in outcome.notes
                     if any(k in n for k in _SUMMARY_NOTES))
        if outcome.reasoning is None:
            failed.append(f"{real}: {outcome.error}")
            continue
        if outcome.critique is None and outcome.critique_error:
            # Scored with counter_evidence "not assessed": the condition is not
            # complete. The failed call was not recorded, so a re-run asks again.
            critique_failed.append(f"{real}: {outcome.critique_error}")
        rows.extend(_score(ctx, cond, world, name, outcome, rendered,
                           2 if name in second else 1))
    directory = ctx.out / code
    directory.mkdir(parents=True, exist_ok=True)
    summary = {
        "condition": code,
        "description": cond.description,
        "tables": len(world_scope),
        "columns": len(rows),
        "second_round": [world.real_table(n) for n in second],
        "failed": failed,
        "critique_failed": critique_failed,
        "notes": notes,
        "calls": _calls(all_calls),
        "wall_s": round(time.perf_counter() - started, 1),
        "hidden": ctx.hide if cond.hide else [],
    }
    (directory / "results.json").write_text(
        json.dumps({"summary": summary, "columns": rows, "tests": tests_run},
                   indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return summary
