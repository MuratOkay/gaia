"""Reasoning orchestration: scope -> skip-by-hash -> reason + critique -> score
-> persist (versioned) -> embed -> report.

Works from the latest stored snapshot, so ``make introspect`` must have run.
The target database is never touched here.

Skip rule (PHASES acceptance, "zero LLM calls when unchanged"): a table is
skipped when its current ``table_semantic`` row has the same
``structure_hash``, reasoning and critique prompt versions, and model as
this run. ``--force`` overrides it. Row-count changes alone never trigger
re-reasoning, because they are data signals (OUTLINE §10).

Concurrency: LLM work runs in worker threads, at most ``llm.concurrency`` at a
time. All database writes happen on the event loop, one table at a time,
with a commit after each table, so an interrupted run keeps the tables it
finished.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings
from gaia.db.models.semantics import ReasoningRun
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.introspect.snapshot import Snapshot, TableSnapshot
from gaia.llm.base import LLMClient
from gaia.llm.cache import KVCache
from gaia.llm.embeddings import EmbeddingClient
from gaia.llm.structured import CallRecord
from gaia.semantics.bundle import TableBundle, build_bundle, build_catalog_index
from gaia.semantics.reasoner import (
    DeclaredJoin,
    ReasonerSettings,
    TableOutcome,
    TableReasoner,
    reasoning_messages,
)
from gaia.semantics.scoring import ColumnScore, score_column, summarize_scores

logger = logging.getLogger(__name__)

__all__ = [
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


@dataclass
class TableResult:
    qualified_name: str
    action: str  # reasoned | skipped | failed | would_reason
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
    distribution: dict[str, Any] = field(default_factory=dict)
    histogram: list[tuple[str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
) -> tuple[dict[str, Any], list[dict[str, Any]], list[ColumnScore]]:
    """Score every column and map the outcome to ``table_semantic`` /
    ``column_semantic`` field dictionaries. Pure; no database access."""
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
        score = score_column(
            col,
            bundle.facts[col.name],
            bundle,
            crit,
            settings.scoring,
            settings.profiling,
        )
        scores.append(score)
        column_rows.append(
            {
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
    summary = summarize_scores(calibrated, settings.profiling)
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
            "weights_version": settings.scoring.weights_version,
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
    return table_row, column_rows, scores


def _histogram(scores: list[float]) -> list[tuple[str, int]]:
    bins = [0] * 10
    for s in scores:
        bins[min(9, max(0, int(s * 10)))] += 1
    return [(f"{i / 10:.1f}-{(i + 1) / 10:.1f}", n) for i, n in enumerate(bins)]


# -- main entry point -----------------------------------------------------------------


async def run_reasoning(
    settings: Settings,
    session: AsyncSession,
    options: ReasoningOptions,
    *,
    llm_factory: Callable[[], LLMClient],
    embedder: EmbeddingClient | None = None,
    cache: KVCache | None = None,
    snapshot_loader: Callable[[dict], Snapshot] | None = None,
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

    index = build_catalog_index(snapshot)
    reasoner_settings = ReasonerSettings(
        model=model,
        thinking=thinking,
        critique_thinking=critique_thinking,
        max_tokens=settings.llm.max_output_tokens,
        temperature=settings.llm.temperature,
        validation_retries=settings.semantics.validation_retries,
        cache_ttl_s=settings.cache.ttl_s.llm,
    )
    # The client is built lazily: a run that skips every table, or a dry
    # run, needs no API key.
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

    prompt_version = reasoning_prompt().version
    critique_prompt_version = critique_prompt().version

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
    )

    # Plain values only from here on: the session commits after every table.
    data_source_id = data_source.id
    snapshot_id = snap_record.id
    table_ids = {(t.schema, t.name): live_tables[(t.schema, t.name)].id for t in scope}
    current = await store.current_table_semantics(table_ids.values())

    # -- decide what to reason -------------------------------------------------
    todo: list[tuple[TableSnapshot, TableBundle]] = []
    for table in scope:
        bundle = build_bundle(table, dialect=snapshot.dialect)
        cur = current.get(table_ids[(table.schema, table.name)])
        unchanged = (
            cur is not None
            and cur.structure_hash == bundle.structure_hash
            and cur.prompt_version == prompt_version
            and cur.critique_prompt_version == critique_prompt_version
            and cur.model == model
        )
        if unchanged and not options.force:
            report.results.append(
                TableResult(table.qualified_name, "skipped", "unchanged since v" + str(cur.version))
            )
            continue
        if cur is None:
            why = "new"
        elif cur.structure_hash != bundle.structure_hash:
            why = "structure changed"
        elif cur.model != model:
            why = f"model changed ({cur.model} -> {model})"
        elif (cur.prompt_version, cur.critique_prompt_version) != (
            prompt_version,
            critique_prompt_version,
        ):
            why = "prompt changed"
        else:
            why = "forced"
        todo.append((table, bundle))
        if options.dry_run:
            rendered = None
            if options.show_bundles:
                rendered = reasoning_messages(bundle, index)[1].content
            report.results.append(
                TableResult(
                    table.qualified_name,
                    "would_reason",
                    why,
                    columns=len(bundle.facts),
                    rendered=rendered,
                )
            )

    if options.dry_run:
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        return report

    # -- bookkeeping before any LLM call -----------------------------------------
    if todo:
        get_reasoner()  # fail fast on a misconfigured client, before any write
    columns_by_table = {
        tid: {name: (c.id, c.structure_hash) for name, c in cols.items()}
        for tid, cols in (await store.live_columns(table_ids.values())).items()
    }
    report.stale_marked = await store.mark_stale(data_source_id)
    report.fk_hints = await store.sync_fk_join_hints(data_source_id, snapshot)
    run = await store.create_run(
        data_source_id=data_source_id,
        snapshot_id=snapshot_id,
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
        },
        tables_in_scope=len(scope),
        tables_skipped=report.count("skipped"),
    )
    run_id = run.id
    report.run_id = str(run_id)
    await session.commit()

    try:
        await _execute(
            settings,
            session,
            store,
            report,
            todo=todo,
            get_reasoner=get_reasoner,
            embed=embed,
            embedder=embedder,
            run_id=run_id,
            data_source_id=data_source_id,
            snapshot_id=snapshot_id,
            table_ids=table_ids,
            columns_by_table=columns_by_table,
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


async def _execute(
    settings: Settings,
    session: AsyncSession,
    store: SemanticsStore,
    report: ReasoningReport,
    *,
    todo: list[tuple[TableSnapshot, TableBundle]],
    get_reasoner: Callable[[], TableReasoner],
    embed: bool,
    embedder: EmbeddingClient | None,
    run_id: uuid.UUID,
    data_source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    table_ids: dict[tuple[str, str], uuid.UUID],
    columns_by_table: dict[uuid.UUID, dict[str, tuple[uuid.UUID, str]]],
    started: float,
) -> None:
    semaphore = asyncio.Semaphore(max(1, settings.llm.concurrency))

    async def work(table: TableSnapshot, bundle: TableBundle) -> tuple[TableSnapshot, TableOutcome]:
        async with semaphore:
            return table, await asyncio.to_thread(get_reasoner().run, bundle)

    columns_written = 0
    failed = 0
    critiques_failed = 0
    tasks = [asyncio.create_task(work(t, b)) for t, b in todo]
    try:
        for next_done in asyncio.as_completed(tasks):
            table, outcome = await next_done
            table_id = table_ids[(table.schema, table.name)]
            await store.record_calls(
                outcome.calls, run_id=run_id, data_source_id=data_source_id, table_id=table_id
            )
            result = TableResult(table.qualified_name, "failed", calls=outcome.calls)

            if not outcome.ok:
                failed += 1
                result.detail = outcome.error or "unknown error"
                logger.warning("reasoning failed for %s: %s", table.qualified_name, result.detail)
            else:
                cols = columns_by_table.get(table_id, {})
                table_row, column_rows, _scores = build_rows(
                    outcome,
                    settings=settings,
                    data_source_id=data_source_id,
                    table_id=table_id,
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    column_ids={name: cid for name, (cid, _h) in cols.items()},
                    column_hashes={name: h for name, (_cid, h) in cols.items()},
                    prompt_version=report.prompt_version,
                    critique_prompt_version=report.critique_prompt_version,
                    model=report.model,
                    thinking=report.thinking,
                )
                await store.write_table_version(table_row, column_rows)
                report.inferred_hints += await store.upsert_inferred_join_hints(
                    data_source_id,
                    outcome.join_hints,
                    run_id=run_id,
                    prompt_version=report.prompt_version,
                    model=report.model,
                )
                columns_written += len(column_rows)
                result.action = "reasoned"
                result.columns = len(column_rows)
                result.critique_ok = outcome.critique is not None
                result.mean_score = table_row["calibrated_score"]
                if outcome.critique is None:
                    critiques_failed += 1
                    result.detail = f"critique failed: {outcome.critique_error}"
                report.notes.extend(f"{table.qualified_name}: {n}" for n in outcome.notes)

            report.results.append(result)
            await session.commit()
    finally:
        for task in tasks:
            task.cancel()

    # -- embeddings ------------------------------------------------------------------------
    scope_ids = list(table_ids.values())
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
    report.distribution = summarize_scores(values, settings.profiling)
    report.histogram = _histogram(values)
    report.wall_ms = int((time.perf_counter() - started) * 1000)

    totals = report.totals()
    run = await session.get(ReasoningRun, run_id)
    assert run is not None
    run.status = "completed" if failed == 0 and critiques_failed == 0 else "completed_with_failures"
    if todo and failed == len(todo):
        run.status = "failed"
    run.finished_at = datetime.now(UTC)
    run.tables_reasoned = report.count("reasoned")
    run.tables_failed = failed
    run.critiques_failed = critiques_failed
    run.columns_written = columns_written
    run.embeddings_written = report.embeddings_written
    run.llm_calls = totals["llm_calls"]
    run.cache_hits = totals["cache_hits"]
    run.prompt_tokens = totals["prompt_tokens"]
    run.completion_tokens = totals["completion_tokens"]
    run.reasoning_tokens = totals["reasoning_tokens"]
    run.cached_tokens = totals["cached_tokens"]
    run.wall_ms = report.wall_ms
    run.notes = report.notes
    await session.commit()
