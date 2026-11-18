"""Reasoning orchestration: scope -> skip-by-hash -> reason + critique -> score
-> persist (versioned) -> embed -> report.

Works from the latest stored snapshot, so ``make introspect`` must have run.
The target database is never touched here.

Skip rule (PHASES acceptance, "zero LLM calls when unchanged"): a table is
skipped when its current ``table_semantic`` row has the same
``structure_hash``, reasoning and critique prompt versions, and model as
this run. ``--force`` overrides it. Row-count changes alone never trigger
re-reasoning, because they are data signals (OUTLINE §10).

Phase 3B (prompt generation 2, ``semantics.prompt_major``): every table gets
an evidence block rendered from an ``EvidenceSource`` — by default the stored
Phase 3A profiles, tests and view lineage when profiling is enabled for the
target, else catalog only — its hash joins the skip key — so a table is re-reasoned
exactly when its evidence changes meaningfully — and columns are scored with
``heuristic-v2`` from an evidence ledger that is stored with the version.
Columns the machine cannot resolve go to ``semantics.review_item``. Views are
reasoned after tables.

Concurrency: LLM work runs in worker threads, at most ``llm.concurrency`` at a
time. All database writes happen on the event loop, one table at a time,
with a commit after each table, so an interrupted run keeps the tables it
finished.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings
from gaia.db.locks import DataSourceBusy, DataSourceLock
from gaia.db.models.semantics import ReasoningRun
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.introspect.snapshot import Snapshot, TableSnapshot
from gaia.llm.base import LLMClient
from gaia.llm.cache import KVCache
from gaia.llm.embeddings import EmbeddingClient
from gaia.llm.structured import CallRecord
from gaia.profiling.summary import EvidenceSource, NoEvidence, TableEvidenceInput
from gaia.semantics.bundle import TableBundle, build_bundle, build_catalog_index
from gaia.semantics.chunking import chunk_bundle
from gaia.semantics.evidence import (
    Exposure,
    Ledger,
    RenderedEvidence,
    build_ledger,
    column_coverage,
    detected_type,
    is_unused,
    next_stage,
    pending_claim_tests,
    render_evidence,
)
from gaia.semantics.reasoner import (
    DeclaredJoin,
    ReasonerSettings,
    TableOutcome,
    TableReasoner,
    ValidatedJoin,
    reasoning_messages,
)
from gaia.semantics.review import importance, review_reasons
from gaia.semantics.schemas import ColumnCritique, TableCritique, TableReasoning
from gaia.semantics.scoring import ColumnScore, score_column, score_column_v2, summarize_scores

logger = logging.getLogger(__name__)

__all__ = [
    "ReasoningBusy",
    "ReasoningOptions",
    "ReasoningReport",
    "TableResult",
    "run_reasoning",
    "select_tables",
]


class ScopeError(ValueError):
    """The requested scope does not match any table in the snapshot."""


@dataclass
class ReasoningOptions:
    target: str | None = None
    schemas: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)  # "schema.table" or "table"
    with_neighbours: bool = False
    limit: int | None = None
    thinking: bool | None = None  # None = config (llm.thinking.semantics)
    critique_thinking: bool | None = None  # None = config (llm.thinking.critique)
    dry_run: bool = False
    force: bool = False
    embed: bool | None = None  # None = config (semantics.embed_after_reasoning)
    show_bundles: bool = False
    # make rescore: never call the model; score the stored answers against
    # today's evidence (with force: every table, even with unchanged inputs)
    rescore_only: bool = False
    # Phase 7C: asked before each table's model call; a reason stops the run there (the
    # table is not started — a scheduled run's budget, its window, a cancellation)
    stop: Any = None
    # Phase 7D: the model's answers for the tables in scope, written nowhere (a model trial);
    # give it with force (every table asked); no lock is taken
    shadow: bool = False


@dataclass
class TableResult:
    qualified_name: str
    # reasoned | rescored | skipped | failed | needs_reasoning | would_reason | would_rescore
    action: str
    detail: str = ""
    columns: int = 0
    critique_ok: bool | None = None
    mean_score: float | None = None
    calls: list[CallRecord] = field(default_factory=list)
    rendered: str | None = None  # dry-run: the user message


@dataclass
class ReasoningReport:
    run_id: str | None
    data_source: str
    snapshot_id: str
    model: str
    prompt_version: str
    critique_prompt_version: str
    thinking: bool
    critique_thinking: bool
    dry_run: bool
    results: list[TableResult] = field(default_factory=list)
    wall_ms: int = 0
    embeddings_written: int = 0
    embedding_cache_hits: int = 0
    stale_marked: dict[str, int] = field(default_factory=dict)
    fk_hints: dict[str, int] = field(default_factory=dict)
    inferred_hints: int = 0
    staled_hints: int = 0  # model hints the new answers no longer propose
    distribution: dict[str, Any] = field(default_factory=dict)
    histogram: list[tuple[str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    prompt_major: int = 1
    exposure: str | None = None
    review: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, int] = field(default_factory=dict)
    rescore_only: bool = False
    # 7D: table -> {"outcome": TableOutcome, "seconds": float} (``ReasoningOptions.shadow``)
    shadow: dict[str, Any] = field(default_factory=dict)

    def count(self, action: str) -> int:
        return sum(1 for r in self.results if r.action == action)

    @property
    def calls(self) -> list[CallRecord]:
        return [c for r in self.results for c in r.calls]

    def totals(self) -> dict[str, int]:
        calls = self.calls
        live = [c for c in calls if not c.cache_hit]
        return {
            "llm_calls": len(live),
            "cache_hits": sum(1 for c in calls if c.cache_hit),
            "prompt_tokens": sum(c.usage.prompt_tokens for c in live),
            "completion_tokens": sum(c.usage.completion_tokens for c in live),
            "reasoning_tokens": sum(c.usage.reasoning_tokens for c in live),
            "cached_tokens": sum(c.usage.cached_tokens for c in live),
            "latency_ms": sum(c.latency_ms for c in live),
        }


# -- scope -----------------------------------------------------------------------


def select_tables(
    snapshot: Snapshot,
    *,
    schemas: list[str] | None = None,
    tables: list[str] | None = None,
    with_neighbours: bool = False,
    limit: int | None = None,
) -> list[TableSnapshot]:
    """Resolve the CLI scope against the snapshot.

    ``tables`` entries are ``schema.table`` or a bare ``table`` (matching every
    schema). Unknown names raise ``ScopeError`` rather than silently shrinking
    the run. ``with_neighbours`` adds tables linked by a declared FK in either
    direction, i.e. PHASES decision 1, option (c).
    """
    by_name = {t.qualified_name.lower(): t for t in snapshot.tables}
    selected: dict[str, TableSnapshot] = {}

    if tables:
        unknown: list[str] = []
        for raw in tables:
            key = raw.strip().lower()
            if "." in key:
                match = [by_name[key]] if key in by_name else []
            else:
                match = [t for t in snapshot.tables if t.name.lower() == key]
            if not match:
                unknown.append(raw)
            for t in match:
                selected[t.qualified_name] = t
        if unknown:
            raise ScopeError(f"unknown table(s): {', '.join(unknown)}")
    else:
        selected = {t.qualified_name: t for t in snapshot.tables}

    if schemas:
        wanted = {s.lower() for s in schemas}
        known = {t.schema.lower() for t in snapshot.tables}
        missing = sorted(wanted - known)
        if missing:
            raise ScopeError(f"unknown schema(s): {', '.join(missing)}")
        selected = {k: t for k, t in selected.items() if t.schema.lower() in wanted}

    if with_neighbours and selected:
        core = set(selected)
        for t in snapshot.tables:
            for rel in t.relations:
                src = f"{rel.from_schema}.{rel.from_table}"
                dst = f"{rel.to_schema}.{rel.to_table}"
                if src in core and dst.lower() in by_name:
                    selected.setdefault(by_name[dst.lower()].qualified_name, by_name[dst.lower()])
                if dst in core and src.lower() in by_name:
                    selected.setdefault(by_name[src.lower()].qualified_name, by_name[src.lower()])

    ordered = [selected[k] for k in sorted(selected)]
    if limit is not None:
        ordered = ordered[: max(0, limit)]
    return ordered


def _fk_degree(snapshot: Snapshot) -> dict[str, int]:
    """Declared foreign keys touching each table, in either direction."""
    degree: dict[str, int] = {}
    for t in snapshot.tables:
        for rel in t.relations:
            for name in (f"{rel.from_schema}.{rel.from_table}", f"{rel.to_schema}.{rel.to_table}"):
                degree[name] = degree.get(name, 0) + 1
    return degree


def declared_joins(snapshot: Snapshot) -> set[DeclaredJoin]:
    return {
        (
            f"{r.from_schema}.{r.from_table}",
            tuple(r.from_columns),
            f"{r.to_schema}.{r.to_table}",
            tuple(r.to_columns),
        )
        for t in snapshot.tables
        for r in t.relations
    }


# -- persistence mapping ---------------------------------------------------------------


def _critique_dict(crit) -> dict[str, Any] | None:
    if crit is None:
        return None
    return {
        "verdict": crit.verdict,
        "counter_evidence": crit.counter_evidence,
        "evidence": crit.evidence,
        "better_interpretation": crit.better_interpretation,
    }


@dataclass
class EvidenceContext:
    """What the v2 path knows about one table besides its bundle."""

    rendered: RenderedEvidence
    source_input: TableEvidenceInput | None
    profiling_available: bool
    table_rows: int | None
    fk_degree: int


@dataclass
class ColumnExtras:
    """Per column, for persistence after the version is written."""

    ledger: Ledger | None = None
    review: dict[str, Any] | None = None


def build_rows(
    outcome: TableOutcome,
    *,
    settings: Settings,
    data_source_id: uuid.UUID,
    table_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    run_id: uuid.UUID,
    column_ids: dict[str, uuid.UUID],
    column_hashes: dict[str, str],
    prompt_version: str,
    critique_prompt_version: str,
    model: str,
    thinking: bool,
    context: EvidenceContext | None = None,
    extras: dict[str, ColumnExtras] | None = None,
    calibrator: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[ColumnScore]]:
    """Score every column and map the outcome to ``table_semantic`` /
    ``column_semantic`` field dictionaries. Pure; no database access.

    With ``context`` (Phase 3B) columns are scored with heuristic-v2 from an
    evidence ledger; ``extras`` is filled with each column's ledger and review
    decision for the caller to persist. ``calibrator``: the fitted calibrator in use for
    ``model`` (6C), else the identity.
    """
    assert outcome.reasoning is not None
    bundle: TableBundle = outcome.bundle
    reasoning = outcome.reasoning
    critiques = {c.name: c for c in (outcome.critique.columns if outcome.critique else [])}
    locale = settings.app.locale

    column_rows: list[dict[str, Any]] = []
    scores: list[ColumnScore] = []
    for col in reasoning.columns:
        if col.name not in column_ids:
            outcome.notes.append(f"column {col.name} has no live catalog row; skipped")
            continue
        crit = critiques.get(col.name)
        facts = bundle.facts[col.name]
        v2: dict[str, Any] = {}
        if context is not None and settings.scoring.version == "heuristic-v2":
            ev_in = context.source_input
            ledger = build_ledger(col, facts, bundle, ev_in, settings.scoring.v2.strengths)
            score = score_column_v2(
                col, facts, bundle, crit, settings.scoring, settings.profiling,
                ledger=ledger, evidence_refs=context.rendered.refs.get(col.name, set()),
                alias_names=[a.alias for a in ev_in.aliases.get(col.name, [])] if ev_in else None,
                calibrator=calibrator,
                accept=(settings.calibration.accept_p, settings.calibration.accept_lower),
            )
            summary = ev_in.columns.get(col.name) if ev_in is not None else None
            coverage = column_coverage(col.name, ev_in)
            unused = is_unused(summary)
            pending = pending_claim_tests(
                col.role, facts, ev_in,
                [f"{h.to_schema}.{h.to_table}.{h.to_columns[0]}"
                 for h in outcome.join_hints
                 if h.from_columns == [col.name] and len(h.to_columns) == 1],
            ) if context.profiling_available else []
            exhausted = next_stage(coverage, ev_in, context.profiling_available, pending) is None
            reasons = review_reasons(
                score, ledger=ledger, critique=crit, exhausted=exhausted, unused=unused,
                bands=settings.profiling,
            )
            weight = importance(facts, context.table_rows, context.fk_degree)
            if extras is not None:
                extras[col.name] = ColumnExtras(
                    ledger=ledger,
                    review=(
                        {
                            "reasons": reasons,
                            "score": score.calibrated_score,
                            "importance": weight,
                            "priority": round((1 - score.calibrated_score) * weight, 6),
                            "coverage": coverage,
                        }
                        if reasons
                        else None
                    ),
                )
            v2 = {
                "semantic_type": detected_type(summary),
                "claimed_semantic_type": col.semantic_type,
                "coverage": coverage,
                "unused": unused,
            }
        else:
            score = score_column(
                col,
                facts,
                bundle,
                crit,
                settings.scoring,
                settings.profiling,
            )
        scores.append(score)
        column_rows.append(
            v2
            | {
                "table_id": table_id,
                "column_id": column_ids[col.name],
                "run_id": run_id,
                "status": score.status,
                "band": score.band,
                "locale": locale,
                "structure_hash": column_hashes[col.name],
                "table_structure_hash": bundle.structure_hash,
                "prompt_version": prompt_version,
                "model": model,
                "business_name": col.business_name,
                "purpose": col.purpose,
                "role": col.role,
                "entity": col.entity,
                "unit": col.unit,
                "value_domain": col.value_domain.model_dump(),
                "synonyms": col.synonyms,
                "example_questions": col.example_questions,
                "assumptions": col.assumptions,
                "unknowns": col.unknowns,
                "alternatives": [a.model_dump() for a in col.alternatives],
                "evidence_refs": col.evidence,
                "self_confidence": col.confidence,
                "critique": _critique_dict(crit),
                "raw_score": score.raw_score,
                "calibrated_score": score.calibrated_score,
                "score_breakdown": score.breakdown(),
                "weights_version": score.weights_version,
                "calibrator": score.calibrator,
            }
        )

    raw = [s.raw_score for s in scores]
    calibrated = [s.calibrated_score for s in scores]
    summary = summarize_scores(calibrated, settings.profiling, [s.band for s in scores]
                               if any(s.lower is not None for s in scores) else None)
    all_approved = bool(scores) and all(s.status == "auto_approved" for s in scores)
    table = reasoning.table
    table_row = {
        "data_source_id": data_source_id,
        "table_id": table_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "status": "auto_approved" if all_approved else "draft",
        "locale": locale,
        "structure_hash": bundle.structure_hash,
        "prompt_version": prompt_version,
        "critique_prompt_version": critique_prompt_version,
        "model": model,
        "thinking": thinking,
        "business_name": table.business_name,
        "purpose": table.purpose,
        "grain": table.grain or None,
        "entity": table.entity,
        "synonyms": table.synonyms,
        "assumptions": table.assumptions,
        "unknowns": table.unknowns,
        "self_confidence": table.confidence,
        # Table score = mean of its column scores (documented aggregate; the
        # table's own self-report is kept separately).
        "raw_score": round(sum(raw) / len(raw), 6) if raw else None,
        "calibrated_score": round(sum(calibrated) / len(calibrated), 6) if calibrated else None,
        "score_breakdown": {
            "aggregate": "mean_of_columns",
            "columns": summary,
            "weights_version": scores[0].weights_version if scores else None,
            "calibrator": settings.scoring.calibrator,
        },
        "critique": (
            {
                "verdict": outcome.critique.table_verdict,
                "notes": outcome.critique.table_notes,
            }
            if outcome.critique
            else {"verdict": "not_assessed", "error": outcome.critique_error}
        ),
        "payload": {
            "reasoning": reasoning.model_dump(),
            "join_hints_kept": [h.__dict__ for h in outcome.join_hints],
            "notes": outcome.notes,
        },
    }
    if context is not None:
        table_row.update(
            evidence_hash=context.rendered.hash,
            exposure=context.rendered.exposure,
            chunks=outcome.chunks,
        )
    return table_row, column_rows, scores


# -- re-scoring and inheritance (2026-11-15) ---------------------------------------------

# Bump when the heuristic-v2 ledger or scorer changes what a stored answer scores
# (not what the model sees): every current version is then re-scored on the next
# `make reason` / `make rescore`, without an LLM call.
#   v2.1 (2026-11-15): FK / inclusion roles (D40), specific-before-generic
#   semantic_type (D6), inconclusive tests, pass-through view columns inherit.
#   v2.2 (2026-11-15): a failed test of one parent no longer contradicts a
#   reference whose values were found in another.
SCORER_REVISION = "v2.2"

_VERDICTS = ("supported", "plausible", "doubtful", "contradicted")
_MAX_LINEAGE_HOPS = 5


@dataclass
class Inherited:
    """A pass-through view column and the table column whose meaning it takes."""

    column: str
    base: str  # schema.table.column (a table, after following view-on-view lineage)
    base_column_id: uuid.UUID
    semantic: Any = None  # the base's current ColumnSemantic, once known


@dataclass
class PlanItem:
    table: TableSnapshot
    bundle: TableBundle
    context: EvidenceContext | None
    action: str  # reason | rescore | skip | needs_reasoning
    why: str
    current: Any = None  # TableSemantic
    focus: list[str] = field(default_factory=list)  # columns the model answers
    inherit: dict[str, Inherited] = field(default_factory=dict)
    score_inputs: str | None = None


def _evidence_material(context: EvidenceContext) -> Any:
    """What the ledger and the queue read from the evidence: the rendered,
    rounded evidence (as in the reasoning skip key, so sub-bucket noise changes
    nothing) plus what the model is not shown but the ledger uses — a test's
    origin, specificity and whether it ran."""
    ev = context.source_input
    tests = {}
    if ev is not None:
        tests = {
            column: sorted(
                (t.test_id, t.origin, t.passed, t.specific, t.conclusive) for t in items
            )
            for column, items in ev.tests.items()
        }
    return {"rendered": context.rendered.stable_projection(), "tests": tests}


def score_inputs_hash(
    settings: Settings,
    context: EvidenceContext,
    inherit: dict[str, Inherited],
    calibrator: str | None = None,
) -> str:
    """Everything a stored answer's scores, queue items and inherited columns
    depend on besides the answer itself. A change re-scores (no LLM call).
    ``calibrator``: the version of the fitted calibrator in use for the answer's model
    (6C) — absent while it is the identity, so a store without one keeps its hashes."""
    p = settings.profiling
    material = {
        "revision": SCORER_REVISION,
        "scoring": settings.scoring.model_dump(mode="json"),
        "bands": [p.accept_at, p.deep_profile_below, p.review_required_below],
        "evidence": _evidence_material(context),
        "profiling_available": context.profiling_available,
        # importance uses log10(rows): a coarse bucket, so row-count drift does
        # not produce a new version after every introspection
        "rows": round(math.log10((context.table_rows or 0) + 1), 1),
        "fk_degree": context.fk_degree,
        "inherit": sorted(
            (name, i.base, str(i.semantic.id) if i.semantic is not None else None)
            for name, i in inherit.items()
        ),
    }
    if calibrator:
        c = settings.calibration
        material["calibrator"] = [calibrator, c.accept_p, c.accept_lower]
    text = json.dumps(material, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _answered(cur) -> set[str]:
    payload = (cur.payload or {}) if cur is not None else {}
    reasoning = payload.get("reasoning") or {}
    return {c.get("name") for c in reasoning.get("columns") or [] if c.get("name")}


def stored_outcome(cur, bundle: TableBundle, column_sems: dict[str, Any]) -> TableOutcome:
    """The stored answer (and critique) of the current version, as an outcome
    that can be scored again. ``column_sems``: column name -> current version."""
    payload = cur.payload or {}
    outcome = TableOutcome(bundle=bundle, chunks=cur.chunks or 1)
    data = payload.get("reasoning")
    if not data:
        return outcome
    reasoning = TableReasoning.model_validate(data)
    wanted = set(bundle.column_names)
    reasoning.columns = [c for c in reasoning.columns if c.name in wanted]
    outcome.reasoning = reasoning
    crit = cur.critique or {}
    if crit.get("verdict") in _VERDICTS:
        columns = [
            ColumnCritique(name=name, **sem.critique)
            for name, sem in column_sems.items()
            if name in wanted and sem.critique
        ]
        outcome.critique = TableCritique(
            table_verdict=crit["verdict"], table_notes=crit.get("notes") or [], columns=columns
        )
    else:
        outcome.critique_error = crit.get("error") or "critique not assessed"
    outcome.join_hints = [
        ValidatedJoin(**h) for h in payload.get("join_hints_kept") or []
        if set(h.get("from_columns") or []) <= wanted  # not from an inherited column
    ]
    return outcome


def inherited_row(
    item: Inherited,
    *,
    table_id: uuid.UUID,
    column_id: uuid.UUID,
    run_id: uuid.UUID,
    column_hash: str,
    table_structure_hash: str,
    prompt_version: str,
    model: str,
    locale: str,
) -> dict[str, Any]:
    """A pass-through view column takes its base column's current meaning and
    score; no LLM call, no ledger of its own, never queued (owner decision
    2026-11-15: the base column has been reasoned, the view adds no meaning)."""
    base = item.semantic
    return {
        "table_id": table_id,
        "column_id": column_id,
        "run_id": run_id,
        # an SME's rejection travels too: the view must not present a meaning the SME
        # rejected (D73)
        "status": base.status if base.status in ("auto_approved", "sme_reviewed",
                                                 "sme_rejected") else "draft",
        "band": base.band,
        "locale": locale,
        "structure_hash": column_hash,
        "table_structure_hash": table_structure_hash,
        "prompt_version": prompt_version,
        "model": model,
        "business_name": base.business_name,
        "purpose": base.purpose,
        "role": base.role,
        "entity": base.entity,
        "unit": base.unit,
        "value_domain": base.value_domain,
        "synonyms": list(base.synonyms or []),
        "example_questions": list(base.example_questions or []),
        "assumptions": list(base.assumptions or []),
        "unknowns": list(base.unknowns or []),
        "alternatives": list(base.alternatives or []),
        "evidence_refs": [f"lin:{item.column}"],
        "self_confidence": base.self_confidence,
        "critique": None,
        "raw_score": base.raw_score,
        "calibrated_score": base.calibrated_score,
        "score_breakdown": {
            "inherited_from": item.base,
            "base_semantic_id": str(base.id),
            "base_version": base.version,
        },
        "weights_version": base.weights_version,
        "calibrator": base.calibrator,
        "semantic_type": base.semantic_type,
        "claimed_semantic_type": base.claimed_semantic_type,
        "coverage": "modules",
        "unused": bool(base.unused),
    }


def _table_aggregate(table_row: dict[str, Any], column_rows: list[dict[str, Any]],
                     settings: Settings) -> None:
    """Table score = mean of all its column scores (inherited ones included)."""
    raw = [r["raw_score"] for r in column_rows]
    calibrated = [r["calibrated_score"] for r in column_rows]
    table_row["raw_score"] = round(sum(raw) / len(raw), 6) if raw else None
    table_row["calibrated_score"] = (
        round(sum(calibrated) / len(calibrated), 6) if calibrated else None
    )
    breakdown = dict(table_row.get("score_breakdown") or {})
    calibrated_bands = [r["band"] for r in column_rows] if any(
        (r.get("calibrator") or "identity") != "identity" for r in column_rows) else None
    breakdown["columns"] = summarize_scores(calibrated, settings.profiling, calibrated_bands)
    table_row["score_breakdown"] = breakdown
    approved = bool(column_rows) and all(r["status"] == "auto_approved" for r in column_rows)
    table_row["status"] = "auto_approved" if approved else "draft"


def inherited_table_row(
    table: TableSnapshot,
    bundle: TableBundle,
    context: EvidenceContext,
    inherit: dict[str, Inherited],
    base_tables: dict[str, Any],
    **fields: Any,
) -> dict[str, Any]:
    """Table-level meaning of a view whose every column passes a base column
    through: stated from the base tables, without an LLM call."""
    bases = sorted({i.base.rsplit(".", 1)[0] for i in inherit.values()})
    single = base_tables.get(bases[0]) if len(bases) == 1 else None
    name = (
        f"{single.business_name} (view)" if single is not None
        else f"View over {', '.join(bases)}"
    )
    purpose = (
        f"Shows columns of {', '.join(bases)} under other names, computing nothing; "
        "each column means what its source column means."
    )
    return fields | {
        "status": "draft",
        "structure_hash": bundle.structure_hash,
        "business_name": name,
        "purpose": purpose,
        "grain": single.grain if single is not None else None,
        "entity": single.entity if single is not None else None,
        "synonyms": [],
        "assumptions": [],
        "unknowns": [],
        "self_confidence": single.self_confidence if single is not None else 0.5,
        "raw_score": None,
        "calibrated_score": None,
        "score_breakdown": {"aggregate": "mean_of_columns", "inherited_from": bases,
                            "weights_version": "heuristic-v2"},
        "critique": {"verdict": "inherited", "notes": ["pass-through view: nothing to critique"]},
        "payload": {"reasoning": None, "inherited": {n: i.base for n, i in inherit.items()},
                    "join_hints_kept": [], "notes": []},
        "evidence_hash": context.rendered.hash,
        "exposure": context.rendered.exposure,
        "chunks": 0,
    }


def _histogram(scores: list[float]) -> list[tuple[str, int]]:
    bins = [0] * 10
    for s in scores:
        bins[min(9, max(0, int(s * 10)))] += 1
    return [(f"{i / 10:.1f}-{(i + 1) / 10:.1f}", n) for i, n in enumerate(bins)]


# -- main entry point -----------------------------------------------------------------


@dataclass
class _Run:
    """Everything the planning and execution steps share."""

    settings: Settings
    session: AsyncSession
    store: SemanticsStore
    options: ReasoningOptions
    report: ReasoningReport
    snapshot: Snapshot
    evidence_source: EvidenceSource
    exposure: Exposure
    fk_degree: dict[str, int]
    live_tables: dict[tuple[str, str], Any]
    table_ids: dict[tuple[str, str], uuid.UUID]
    index: Any
    major: int
    model: str
    prompt_version: str
    critique_prompt_version: str
    # the fitted calibrators in use (6C), by (model, scoring version)
    calibrators: dict[tuple[str, str], Any] = field(default_factory=dict)

    def calibrator(self, model: str) -> Any:
        # P6-9: a meaning written under a model's earlier name is the renamed model's
        name = self.settings.calibration.canonical(model) or model
        return self.calibrators.get((name, V2_SCORING))

    def calibrator_version(self, model: str) -> str | None:
        fit = self.calibrator(model)
        return fit.version if fit is not None else None


V2_SCORING = "heuristic-v2"  # the weights version score_column_v2 writes


async def _calibrators(session: AsyncSession) -> dict[tuple[str, str], Any]:
    """The meaning calibrators in use (6C); none before migration 0020 or when none is."""
    from gaia.evaluation.calibrators import in_use

    return await in_use(session, "meaning")


class ReasoningBusy(DataSourceBusy):
    """Another reasoning or re-scoring run holds this data source (5.0, T2)."""


async def run_reasoning(
    settings: Settings,
    session: AsyncSession,
    options: ReasoningOptions,
    *,
    llm_factory: Callable[[], LLMClient],
    embedder: EmbeddingClient | None = None,
    cache: KVCache | None = None,
    snapshot_loader: Callable[[dict], Snapshot] | None = None,
    evidence_source: EvidenceSource | None = None,
) -> ReasoningReport:
    """Reason (or re-score) a data source. One writing run per data source at a time:
    the plan is made under the lock, so two runs can never plan from the same state.
    A dry run writes nothing and takes no lock."""
    kwargs = {"llm_factory": llm_factory, "embedder": embedder, "cache": cache,
              "snapshot_loader": snapshot_loader, "evidence_source": evidence_source}
    if options.dry_run or options.shadow:
        return await _run_reasoning(settings, session, options, **kwargs)
    label = options.target or settings.app.default_target
    data_source = await SemanticsStore(session).data_source(label)
    if data_source is None:
        raise LookupError(f"no data source {label!r} in the metadata store; run `make introspect`")
    async with DataSourceLock(
        session, data_source.id, "reasoning", error=ReasoningBusy,
        message=(f"another reasoning or re-scoring run holds {label!r}; "
                 "wait for it to finish (one run per data source)"),
    ):
        return await _run_reasoning(settings, session, options, **kwargs)


async def _run_reasoning(
    settings: Settings,
    session: AsyncSession,
    options: ReasoningOptions,
    *,
    llm_factory: Callable[[], LLMClient],
    embedder: EmbeddingClient | None = None,
    cache: KVCache | None = None,
    snapshot_loader: Callable[[dict], Snapshot] | None = None,
    evidence_source: EvidenceSource | None = None,
) -> ReasoningReport:
    started = time.perf_counter()
    store = SemanticsStore(session)
    label = options.target or settings.app.default_target

    if snapshot_loader is None:
        # Deferred: gaia.introspect.service pulls in the dialect registry.
        from gaia.introspect.service import snapshot_from_document as snapshot_loader

    data_source = await store.data_source(label)
    if data_source is None:
        raise LookupError(f"no data source {label!r} in the metadata store; run `make introspect`")
    snap_record = await store.latest_snapshot(data_source.id)
    if snap_record is None:
        raise LookupError(f"no snapshot for {label!r}; run `make introspect`")

    snapshot = snapshot_loader(snap_record.document)
    live_tables = await store.live_tables(data_source.id)
    scope = [
        t
        for t in select_tables(
            snapshot,
            schemas=options.schemas,
            tables=options.tables,
            with_neighbours=options.with_neighbours,
            limit=options.limit,
        )
        if (t.schema, t.name) in live_tables
    ]

    thinking = settings.llm.thinking.semantics if options.thinking is None else options.thinking
    critique_thinking = (
        settings.llm.thinking.critique
        if options.critique_thinking is None
        else options.critique_thinking
    )
    embed = settings.semantics.embed_after_reasoning if options.embed is None else options.embed
    model = settings.llm.model

    index = build_catalog_index(snapshot, unique_keys=settings.semantics.prompt_major >= 2)
    reasoner_settings = ReasonerSettings(
        model=model,
        thinking=thinking,
        critique_thinking=critique_thinking,
        max_tokens=settings.llm.max_output_tokens,
        temperature=settings.llm.temperature,
        validation_retries=settings.semantics.validation_retries,
        cache_ttl_s=settings.cache.ttl_s.llm,
        prompt_major=settings.semantics.prompt_major,
        context_budget_tokens=settings.llm.context_budget_tokens,
    )
    major = settings.semantics.prompt_major
    if evidence_source is None and major >= 2:
        # Deferred: gaia.profiling.service imports this module.
        from gaia.profiling.service import load_profile_evidence, profiling_enabled

        if profiling_enabled(settings, label):
            # Phase 3A: stored profiles, tests and view lineage. Tables not yet
            # profiled are catalog-only with more evidence still to come.
            evidence_source = await load_profile_evidence(
                settings, session, label, data_source.id, snapshot
            )
        else:
            # Catalog-only mode: nothing beyond the catalog will come, so
            # coverage is final and low scores go to the review queue.
            evidence_source = NoEvidence()
    if evidence_source is None:
        evidence_source = NoEvidence()
    exposure = Exposure.from_settings(settings)
    # The client is built lazily: a run that skips every table, a re-score or a
    # dry run needs no API key.
    reasoner: TableReasoner | None = None

    def get_reasoner() -> TableReasoner:
        nonlocal reasoner
        if reasoner is None:
            reasoner = TableReasoner(
                llm_factory(),
                reasoner_settings,
                index,
                declared_joins=declared_joins(snapshot),
                cache=cache,
            )
        return reasoner

    from gaia.semantics.critique import critique_prompt
    from gaia.semantics.reasoner import reasoning_prompt

    prompt_version = reasoning_prompt(major).version
    critique_prompt_version = critique_prompt(major).version

    report = ReasoningReport(
        run_id=None,
        data_source=label,
        snapshot_id=str(snap_record.id),
        model=model,
        prompt_version=prompt_version,
        critique_prompt_version=critique_prompt_version,
        thinking=thinking,
        critique_thinking=critique_thinking,
        dry_run=options.dry_run,
        prompt_major=major,
        exposure=exposure.mode if major >= 2 else None,
        rescore_only=options.rescore_only,
    )
    ctx = _Run(
        settings=settings,
        session=session,
        store=store,
        options=options,
        report=report,
        snapshot=snapshot,
        evidence_source=evidence_source,
        exposure=exposure,
        fk_degree=_fk_degree(snapshot),
        live_tables=live_tables,
        table_ids={(t.schema, t.name): live_tables[(t.schema, t.name)].id for t in scope},
        index=index,
        major=major,
        model=model,
        prompt_version=prompt_version,
        critique_prompt_version=critique_prompt_version,
        calibrators=await _calibrators(session),
    )

    # -- decide: tables first; views after the tables they read from -------------
    tables = [t for t in scope if t.kind != "view"]
    views = [t for t in scope if t.kind == "view"]
    first = await _plan(ctx, tables)
    if options.shadow:  # 7D: ask the model, write nothing
        sem = asyncio.Semaphore(max(1, settings.llm.concurrency))

        async def ask(item: PlanItem) -> tuple[PlanItem, TableOutcome, float]:
            async with sem:
                evidence = item.context.rendered if item.context is not None else None
                bundle = _focused(item)
                if evidence is not None and bundle is not item.bundle:
                    evidence = evidence.subset(item.focus)
                t0 = time.perf_counter()
                outcome = await asyncio.to_thread(get_reasoner().run, bundle, evidence)
                return item, outcome, time.perf_counter() - t0

        asked = [i for i in first if i.action == "reason"]
        if asked:
            get_reasoner()
        for item, outcome, secs in await asyncio.gather(*(ask(i) for i in asked)):
            report.shadow[item.table.qualified_name] = {"outcome": outcome, "seconds": secs}
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        return report
    if options.dry_run:
        reasoned = {i.table.qualified_name for i in first if i.action == "reason"}
        second = await _plan(ctx, views, assume_reasoned=reasoned)
        for item in [*first, *second]:
            _report_plan(ctx, item)
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        return report

    # -- bookkeeping before any LLM call -----------------------------------------
    if any(i.action == "reason" for i in first):
        get_reasoner()  # fail fast on a misconfigured client, before any write
    report.stale_marked = await store.mark_stale(data_source.id)
    report.fk_hints = await store.sync_fk_join_hints(data_source.id, snapshot)
    run = await store.create_run(
        data_source_id=data_source.id,
        snapshot_id=snap_record.id,
        status="running",
        model=model,
        prompt_version=prompt_version,
        critique_prompt_version=critique_prompt_version,
        thinking=thinking,
        critique_thinking=critique_thinking,
        options={
            "schemas": options.schemas,
            "tables": options.tables,
            "with_neighbours": options.with_neighbours,
            "limit": options.limit,
            "force": options.force,
            "rescore_only": options.rescore_only,
        },
        tables_in_scope=len(scope),
        tables_skipped=0,
    )
    run_id = run.id
    report.run_id = str(run_id)
    await session.commit()

    try:
        await _execute(
            ctx,
            first=first,
            views=views,
            get_reasoner=get_reasoner,
            embed=embed,
            embedder=embedder,
            run_id=run_id,
            data_source_id=data_source.id,
            snapshot_id=snap_record.id,
            started=started,
        )
    except BaseException as exc:
        await session.rollback()
        failed_run = await session.get(ReasoningRun, run_id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.finished_at = datetime.now(UTC)
            failed_run.notes = [*report.notes, f"aborted: {type(exc).__name__}: {exc}"]
            await session.commit()
        raise
    return report


def _report_plan(ctx: _Run, item: PlanItem) -> None:
    report = ctx.report
    name = item.table.qualified_name
    if item.action == "skip":
        report.results.append(TableResult(name, "skipped", item.why))
        return
    if item.action == "needs_reasoning":
        report.results.append(TableResult(name, "needs_reasoning", item.why))
        return
    rendered = None
    if item.action == "reason" and ctx.options.show_bundles:
        bundle = _focused(item)
        evidence = item.context.rendered if item.context else None
        if evidence is not None and len(item.focus) < len(item.bundle.column_names):
            evidence = evidence.subset(item.focus)
        rendered = reasoning_messages(bundle, ctx.index, evidence, ctx.major)[1].content
    action = "would_reason" if item.action == "reason" else "would_rescore"
    detail = item.why
    if item.inherit:
        detail += f"; {len(item.inherit)} pass-through column(s) inherit"
    report.results.append(
        TableResult(name, action, detail, columns=len(item.focus), rendered=rendered)
    )


def _focused(item: PlanItem) -> TableBundle:
    """The bundle the model answers: all columns, or for a view only those that do
    not inherit (the rest are context, as in a chunk)."""
    if not item.inherit or len(item.focus) == len(item.bundle.column_names):
        return item.bundle
    part = chunk_bundle(item.bundle, item.focus, 1, 1)
    part.document["chunk"] = {
        "part": 1,
        "of": 1,
        "note": "answer only the columns under 'columns'; the other columns pass base "
        "columns through unchanged and take their meanings",
    }
    return part


async def _inheritance(
    ctx: _Run, view: TableSnapshot, ev: TableEvidenceInput | None,
    assume_reasoned: set[str],
) -> dict[str, Inherited]:
    """Pass-through columns of a view whose (ultimate) base table column has a
    meaning, or will have one in this run (dry run)."""
    if view.kind != "view" or ev is None or not ev.lineage:
        return {}
    found: dict[str, tuple[str, str, str]] = {}
    for column in (c.name for c in view.columns):
        source = None
        lineage = ev.lineage.get(column)
        hops = 0
        while lineage is not None and lineage.classification == "pass_through" and (
            lineage.source
        ) and hops < _MAX_LINEAGE_HOPS:
            source = lineage.source
            schema, table, col = source.rsplit(".", 2)
            base = ctx.snapshot.table(schema, table)
            if base is None or base.kind != "view":
                break
            upstream = ctx.evidence_source.table_evidence(f"{schema}.{table}")
            lineage = upstream.lineage.get(col) if upstream is not None else None
            source = None if lineage is None else source
            hops += 1
        if source is None:
            continue
        schema, table, col = source.rsplit(".", 2)
        base = ctx.snapshot.table(schema, table)
        if base is None or base.kind == "view" or (schema, table) not in ctx.live_tables:
            continue
        found[column] = (schema, table, col)
    if not found:
        return {}
    base_ids = {ctx.live_tables[(s, t)].id for s, t, _c in found.values()}
    columns = await ctx.store.live_columns(base_ids)
    semantics = await ctx.store.current_column_semantics(base_ids)
    out: dict[str, Inherited] = {}
    for column, (schema, table, col) in found.items():
        record = columns.get(ctx.live_tables[(schema, table)].id, {}).get(col)
        if record is None:
            continue
        semantic = semantics.get(record.id)
        if semantic is None and f"{schema}.{table}" not in assume_reasoned:
            continue  # the base has no meaning yet: the model reasons this column
        if semantic is not None and semantic.status == "stale":
            continue
        out[column] = Inherited(column, f"{schema}.{table}.{col}", record.id, semantic)
    return out


async def _plan(
    ctx: _Run, tables: list[TableSnapshot], *, assume_reasoned: set[str] | None = None
) -> list[PlanItem]:
    """Per table: reason (LLM), re-score (stored answer, no LLM), or skip."""
    options, store = ctx.options, ctx.store
    current = await store.current_table_semantics(
        ctx.table_ids[(t.schema, t.name)] for t in tables
    )
    items: list[PlanItem] = []
    for table in tables:
        bundle = build_bundle(table, dialect=ctx.snapshot.dialect)
        context: EvidenceContext | None = None
        inherit: dict[str, Inherited] = {}
        if ctx.major >= 2:
            ev_in = ctx.evidence_source.table_evidence(table.qualified_name)
            context = EvidenceContext(
                rendered=render_evidence(bundle, ev_in, ctx.exposure),
                source_input=ev_in,
                # The table's own verdict wins (e.g. SELECT denied on one table).
                profiling_available=bool(
                    ev_in.profiling_available if ev_in is not None
                    else ctx.evidence_source.profiling_available
                ),
                table_rows=table.estimated_rows,
                fk_degree=ctx.fk_degree.get(table.qualified_name, 0),
            )
            inherit = await _inheritance(ctx, table, ev_in, assume_reasoned or set())
        focus = [c for c in bundle.column_names if c not in inherit]
        cur = current.get(ctx.table_ids[(table.schema, table.name)])
        evidence_same = context is None or (
            cur is not None
            and cur.evidence_hash in (context.rendered.hash, context.rendered.legacy_hash)
        )
        # A version whose critique failed is incomplete: its columns cannot be
        # auto-approved, so it is re-run (the reasoning answer usually comes from
        # the response cache; only the critique goes to the model again).
        critique_missing = bool(focus) and cur is not None and (
            not cur.critique or cur.critique.get("verdict") == "not_assessed"
        )
        structure_same = cur is not None and cur.structure_hash == bundle.structure_hash
        covered = set(focus) <= _answered(cur)
        if cur is None:
            why = "new"
        elif not structure_same:
            why = "structure changed"
        elif cur.model != ctx.model:
            why = f"model changed ({cur.model} -> {ctx.model})"
        elif (cur.prompt_version, cur.critique_prompt_version) != (
            ctx.prompt_version, ctx.critique_prompt_version,
        ):
            why = "prompt changed"
        elif not evidence_same:
            why = "evidence changed"
        elif critique_missing:
            why = "critique missing"
        elif not covered:
            why = "columns without an answer"
        elif options.force:
            why = "forced"
        else:
            why = ""
        needs_model = bool(why) and bool(focus)
        if options.rescore_only and needs_model:
            # An explicit re-score scores the stored answer against today's
            # evidence even when the model would see something new, as long as
            # the answer still fits the table.
            if cur is None or not structure_same or not covered:
                items.append(PlanItem(table, bundle, context, "needs_reasoning",
                                      why + ": run make reason", cur, focus, inherit))
                continue
            needs_model = False
            why = f"{why}; re-scored, make reason would re-read it"
        if needs_model:
            items.append(PlanItem(table, bundle, context, "reason", why, cur, focus, inherit))
            continue
        if ctx.major < 2 or context is None:
            items.append(PlanItem(table, bundle, context, "skip",
                                  "unchanged since v" + str(cur.version), cur))
            continue
        inputs = score_inputs_hash(ctx.settings, context, inherit, ctx.calibrator_version(
            cur.model if cur is not None else ctx.model))
        stored = ((cur.score_breakdown or {}).get("score_inputs") if cur is not None else None)
        if cur is not None and stored == inputs and not why and not (
            options.rescore_only and options.force
        ):
            items.append(PlanItem(table, bundle, context, "skip",
                                  "unchanged since v" + str(cur.version), cur, focus, inherit,
                                  inputs))
            continue
        reason = (
            "pass-through view" if not focus else
            why if why else "scoring inputs changed"
        )
        items.append(PlanItem(table, bundle, context, "rescore", reason, cur, focus, inherit,
                              inputs))
    return items


async def _execute(
    ctx: _Run,
    *,
    first: list[PlanItem],
    views: list[TableSnapshot],
    get_reasoner: Callable[[], TableReasoner],
    embed: bool,
    embedder: EmbeddingClient | None,
    run_id: uuid.UUID,
    data_source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    started: float,
) -> None:
    settings, session, store, report = ctx.settings, ctx.session, ctx.store, ctx.report
    semaphore = asyncio.Semaphore(max(1, settings.llm.concurrency))
    columns_by_table = {
        tid: {name: (c.id, c.structure_hash) for name, c in cols.items()}
        for tid, cols in (await store.live_columns(ctx.table_ids.values())).items()
    }
    counters = {"columns": 0, "failed": 0, "critiques_failed": 0, "reasoned_or_rescored": 0}

    async def work(item: PlanItem) -> tuple[PlanItem, TableOutcome | str]:
        async with semaphore:
            why = ctx.options.stop.reason() if ctx.options.stop is not None else None
            if why:  # 7C: not started; the next run takes it up (nothing was written)
                return item, why
            evidence = item.context.rendered if item.context is not None else None
            bundle = _focused(item)
            if evidence is not None and bundle is not item.bundle:
                evidence = evidence.subset(item.focus)
            outcome = await asyncio.to_thread(get_reasoner().run, bundle, evidence)
            return item, outcome

    async def persist(item: PlanItem, outcome: TableOutcome | None, action: str) -> None:
        table = item.table
        table_id = ctx.table_ids[(table.schema, table.name)]
        result = TableResult(table.qualified_name, "failed",
                             calls=outcome.calls if outcome else [])
        if outcome is not None and outcome.calls:
            await store.record_calls(
                outcome.calls, run_id=run_id, data_source_id=data_source_id, table_id=table_id
            )
        if item.focus and (outcome is None or not outcome.ok):
            counters["failed"] += 1
            result.detail = (outcome.error if outcome else None) or "unknown error"
            logger.warning("reasoning failed for %s: %s", table.qualified_name, result.detail)
            report.results.append(result)
            await session.commit()
            return
        cols = columns_by_table.get(table_id, {})
        # A re-scored answer keeps the prompt, model and thinking it was given; a
        # new answer or a pass-through view carries this run's.
        stored = action == "rescored" and bool(item.focus) and item.current is not None
        common = {
            "data_source_id": data_source_id,
            "table_id": table_id,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "locale": settings.app.locale,
            "prompt_version": item.current.prompt_version if stored else ctx.prompt_version,
            "critique_prompt_version": (
                item.current.critique_prompt_version if stored else ctx.critique_prompt_version
            ),
            "model": item.current.model if stored else ctx.model,
            "thinking": item.current.thinking if stored else report.thinking,
        }
        extras: dict[str, ColumnExtras] = {}
        if item.focus:
            assert outcome is not None and outcome.reasoning is not None
            table_row, column_rows, _scores = build_rows(
                outcome,
                settings=settings,
                data_source_id=data_source_id,
                table_id=table_id,
                snapshot_id=snapshot_id,
                run_id=run_id,
                column_ids={name: cid for name, (cid, _h) in cols.items()},
                column_hashes={name: h for name, (_cid, h) in cols.items()},
                prompt_version=common["prompt_version"],
                critique_prompt_version=common["critique_prompt_version"],
                model=common["model"],
                thinking=common["thinking"],
                context=item.context,
                extras=extras,
                calibrator=ctx.calibrator(common["model"]),
            )
            if stored:
                # The model saw what it saw: keep the answer's evidence hash, so a
                # later `make reason` still re-reasons if the model-visible
                # evidence has changed since.
                table_row["evidence_hash"] = item.current.evidence_hash
                table_row["exposure"] = item.current.exposure
        else:
            assert item.context is not None
            base_tables = {}
            bases = {i.base.rsplit(".", 1)[0] for i in item.inherit.values()}
            base_ids = {ctx.live_tables[tuple(b.split(".", 1))].id: b for b in bases
                        if tuple(b.split(".", 1)) in ctx.live_tables}
            for tid, sem in (await store.current_table_semantics(base_ids)).items():
                base_tables[base_ids[tid]] = sem
            table_row = inherited_table_row(
                table, item.bundle, item.context, item.inherit, base_tables, **common
            )
            column_rows = []
        # pass-through view columns: the base column's current meaning
        if item.inherit:
            if any(i.semantic is None for i in item.inherit.values()):
                fresh = await _inheritance(ctx, table, item.context.source_input
                                           if item.context else None, set())
                for name, i in item.inherit.items():
                    if i.semantic is None and name in fresh:
                        i.semantic = fresh[name].semantic
            for name, inh in item.inherit.items():
                if inh.semantic is None or name not in cols:
                    report.notes.append(
                        f"{table.qualified_name}.{name}: base {inh.base} has no meaning yet"
                    )
                    continue
                column_rows.append(inherited_row(
                    inh,
                    table_id=table_id,
                    column_id=cols[name][0],
                    run_id=run_id,
                    column_hash=cols[name][1],
                    table_structure_hash=item.bundle.structure_hash,
                    prompt_version=common["prompt_version"],
                    model=common["model"],
                    locale=settings.app.locale,
                ))
            _table_aggregate(table_row, column_rows, settings)
        if item.context is not None and ctx.major >= 2:
            inputs = score_inputs_hash(settings, item.context, item.inherit,
                                       ctx.calibrator_version(common["model"]))
            table_row["score_breakdown"] = {
                **(table_row.get("score_breakdown") or {}), "score_inputs": inputs
            }
        _sem, column_sems = await store.write_table_version(table_row, column_rows)
        if item.context is not None:
            by_column = {cid: name for name, (cid, _h) in cols.items()}
            ledgers, reviews = {}, {}
            for sem in column_sems:
                name = by_column.get(sem.column_id)
                report.coverage[sem.coverage] = report.coverage.get(sem.coverage, 0) + 1
                extra = extras.get(name) if name else None
                if extra is None:
                    continue
                if extra.ledger is not None:
                    ledgers[sem.id] = extra.ledger.items
                if extra.review is not None:
                    reviews[sem.id] = (sem.column_id, extra.review)
            await store.write_evidence_items(table_id, ledgers)
            queued = await store.replace_review_items(
                data_source_id, table_id, [s.column_id for s in column_sems], reviews
            )
            for reasons in queued:
                for reason in reasons:
                    report.review[reason] = report.review.get(reason, 0) + 1
            report.review["queued"] = report.review.get("queued", 0) + len(queued)
        if (action == "reasoned" and outcome is not None) or item.inherit:
            # A re-scored view drops the hints its inherited columns had proposed:
            # joins belong to the base column now.
            written, staled = await store.upsert_inferred_join_hints(
                data_source_id,
                outcome.join_hints if outcome is not None else [],
                run_id=run_id,
                prompt_version=common["prompt_version"],
                model=common["model"],
                table=(table.schema, table.name),
            )
            report.inferred_hints += written
            report.staled_hints += staled
        counters["columns"] += len(column_rows)
        counters["reasoned_or_rescored"] += 1
        result.action = action
        result.detail = item.why if action == "rescored" else ""
        result.columns = len(column_rows)
        result.critique_ok = outcome.critique is not None if outcome and item.focus else None
        result.mean_score = table_row["calibrated_score"]
        if action == "reasoned" and outcome is not None and outcome.critique is None:
            counters["critiques_failed"] += 1
            result.detail = f"critique failed: {outcome.critique_error}"
        if outcome is not None:
            report.notes.extend(f"{table.qualified_name}: {n}" for n in outcome.notes)
        report.results.append(result)
        await session.commit()

    async def run_wave(items: list[PlanItem]) -> None:
        for item in items:
            if item.action in ("skip", "needs_reasoning"):
                _report_plan(ctx, item)
        # no LLM: stored answers scored again, pass-through views inherited
        for item in (i for i in items if i.action == "rescore"):
            outcome = None
            if item.focus:
                cur = item.current
                sems = await store.current_column_semantics([cur.table_id])
                names = columns_by_table.get(cur.table_id, {})
                by_name = {n: sems[cid] for n, (cid, _h) in names.items() if cid in sems}
                outcome = stored_outcome(cur, _focused(item), by_name)
                if outcome.reasoning is None:
                    report.results.append(TableResult(
                        item.table.qualified_name, "needs_reasoning",
                        "no stored answer to re-score: run make reason"))
                    continue
            await persist(item, outcome, "rescored")
        tasks = [asyncio.create_task(work(i)) for i in items if i.action == "reason"]
        try:
            for next_done in asyncio.as_completed(tasks):
                item, outcome = await next_done
                if isinstance(outcome, str):
                    report.results.append(TableResult(
                        item.table.qualified_name, "stopped", f"not started: {outcome}"))
                    continue
                await persist(item, outcome, "reasoned")
        finally:
            for task in tasks:
                task.cancel()

    # Views after tables: a computed view column means something in terms of its
    # inputs (A8), and a pass-through column takes its base column's meaning.
    await run_wave(first)
    if views:
        second = await _plan(ctx, views)
        if any(i.action == "reason" for i in second):
            get_reasoner()
        await run_wave(second)

    # -- embeddings ------------------------------------------------------------------------
    scope_ids = list(ctx.table_ids.values())
    if embed and embedder is not None:
        targets = await store.embedding_targets(
            data_source_id, settings.embeddings.model, table_ids=scope_ids
        )
        if targets:
            try:
                emb = await asyncio.to_thread(embedder.embed, [t.text for t in targets])
                report.embeddings_written = await store.add_embeddings(
                    targets,
                    emb.vectors,
                    model=settings.embeddings.model,
                    dimensions=emb.dimensions,
                )
                report.embedding_cache_hits = getattr(emb, "cache_hits", 0)
                await session.commit()
            except Exception as exc:  # recorded, not swallowed
                await session.rollback()
                report.notes.append(f"embedding failed: {type(exc).__name__}: {exc}")
                logger.warning("embedding failed: %s", exc)
    elif embed:
        report.notes.append("embedding skipped: no embedding client")

    # -- finish ------------------------------------------------------------------------------
    scored = await store.current_column_scores(scope_ids)
    values = [s for s, _ in scored]
    report.distribution = summarize_scores(values, settings.profiling, [b for _s, b in scored]
                                           if ctx.calibrators else None)
    report.histogram = _histogram(values)
    report.wall_ms = int((time.perf_counter() - started) * 1000)

    failed = counters["failed"]
    totals = report.totals()
    run = await session.get(ReasoningRun, run_id)
    assert run is not None
    attempted = report.count("reasoned") + failed
    run.status = (
        "completed" if failed == 0 and counters["critiques_failed"] == 0
        else "completed_with_failures"
    )
    if attempted and failed == attempted:
        run.status = "failed"
    run.finished_at = datetime.now(UTC)
    run.tables_reasoned = report.count("reasoned")
    run.tables_skipped = report.count("skipped")
    run.tables_failed = failed
    run.critiques_failed = counters["critiques_failed"]
    run.columns_written = counters["columns"]
    run.embeddings_written = report.embeddings_written
    run.llm_calls = totals["llm_calls"]
    run.cache_hits = totals["cache_hits"]
    run.prompt_tokens = totals["prompt_tokens"]
    run.completion_tokens = totals["completion_tokens"]
    run.reasoning_tokens = totals["reasoning_tokens"]
    run.cached_tokens = totals["cached_tokens"]
    run.wall_ms = report.wall_ms
    notes = list(report.notes)
    if report.count("stopped"):
        notes.append(f"stopped: {report.count('stopped')} table(s) not started — "
                     f"{ctx.options.stop.stopped if ctx.options.stop else 'stop'}; the next "
                     "run takes them up")
    if report.count("rescored"):
        notes.append(f"re-scored without an LLM call: {report.count('rescored')} table(s)")
    run.notes = notes
    await session.commit()
