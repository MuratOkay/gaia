"""Evidence collection orchestration (Phase 3A): Stage 1 and Stage 3.

**Stage 1 — light profile** (``run_profiling``), per data source:

1. scope from the latest stored snapshot (same selectors as reasoning);
2. refuse outside the run window unless told otherwise (A5);
3. one lock per data source in the metadata store (PostgreSQL advisory lock):
   one profiling session per target, ever (A6, P3-10);
4. module definitions (views, procedures, functions, triggers) captured and
   view column lineage parsed — catalog reads only;
5. per table: reuse a fresh profile (zero data queries), or skip with a reason
   (``view``, ``catalog_sufficient``, ``no_select``), or profile within the
   page and time budgets (``gaia.profiling.statistics``);
6. join discovery over the profiled tables: candidates from values, one
   inclusion test each, at most ``max_join_tests`` per run;
7. footprint: statistics that appeared on the target during the run.

**Stage 3 — targeted tests** (``run_stage3``), after reasoning: the tests that
apply to the stored claims and have not run (``pending_claim_tests``) — an
inclusion test per join hint, exact uniqueness for identifiers, exact top-k
for codes and flags. Their results change the evidence hash of exactly the
tables they concern, so the next ``make reason`` re-reasons only those.

Target work runs on one worker thread with one target session; metadata
writes happen on the event loop with a commit after each table, so an
interrupted run keeps what it finished.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings, TargetConfig
from gaia.db.locks import DataSourceLock
from gaia.db.models.profiling import ProfileRun
from gaia.db.models.semantics import ColumnSemantic, JoinHint
from gaia.db.repositories.profile_store import ProfileStore
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.introspect.snapshot import Snapshot, TableSnapshot
from gaia.profiling.budget import (
    RunBudget,
    TableBudget,
    catalog_sufficient,
    freshness,
    in_window,
    table_pages,
)
from gaia.profiling.joins import (
    KEEP_RATE,
    family,
    find_candidates,
    inclusion_test,
    integer_density,
    single_keys,
    topk_test,
    uniqueness_test,
)
from gaia.profiling.lineage import parse_view_lineage
from gaia.profiling.statistics import (
    TableProfileResult,
    column_pages,
    join_sample,
    profile_table,
)
from gaia.profiling.summary import ColumnProfileSummary, TableEvidenceInput, VerificationResult
from gaia.semantics.bundle import build_bundle
from gaia.semantics.evidence import pending_claim_tests
from gaia.semantics.service import select_tables

logger = logging.getLogger(__name__)

__all__ = [
    "ProfileOptions",
    "ProfileReport",
    "ProfilingError",
    "load_profile_evidence",
    "profiling_enabled",
    "run_profiling",
    "run_stage3",
]


class ProfilingError(RuntimeError):
    """The run cannot start (disabled, outside the window, locked, no snapshot)."""


@dataclass
class ProfileOptions:
    target: str | None = None
    schemas: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    with_neighbours: bool = False
    limit: int | None = None
    dry_run: bool = False
    ignore_window: bool = False
    force: bool = False  # re-profile fresh tables; Stage 3: re-run claim tests
    joins: bool = True


@dataclass
class TableAction:
    qualified_name: str
    action: str  # profiled | partial | failed | reused | skipped | would_profile | tested
    detail: str = ""
    queries: int = 0
    pages: int = 0
    runtime_ms: int = 0
    size_class: str | None = None
    notes: list[str] = field(default_factory=list)  # every note (detail: the first two)


@dataclass
class ProfileReport:
    data_source: str
    dialect: str
    stage: int
    dry_run: bool
    run_id: str | None = None
    results: list[TableAction] = field(default_factory=list)
    modules: dict[str, int] = field(default_factory=dict)
    lineage: dict[str, int] = field(default_factory=dict)
    joins: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    # Stage 1: samples re-drawn from earlier-profiled tables for new parents (D12)
    join_samples: int = 0
    # Stage 3: what the claim tests did to the model's join hints (refuted / restored)
    hints: Counter[str] = field(default_factory=Counter)
    footprint: dict[str, Any] | None = None
    queries: int = 0  # data queries (read table rows)
    catalog_queries: int = 0
    pages: int = 0
    wall_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for r in self.results if r.action == action)


# -- configuration ---------------------------------------------------------------------------


def _target(settings: Settings, label: str) -> TargetConfig | None:
    """A config target, or a data source added at runtime (4F; D76: profiling looked in the
    config only, so such a data source could be reasoned about but never profiled)."""
    from gaia.datasources.resolver import TargetUnusable

    try:
        return settings.target(label)
    except TargetUnusable as exc:  # a KeyError too: first
        raise ProfilingError(str(exc)) from None
    except KeyError:
        return None


def profiling_enabled(settings: Settings, label: str) -> bool:
    target = _target(settings, label)
    return bool(settings.profiling.enabled and (target is None or target.profiling_enabled))


def _window(settings: Settings, target: TargetConfig) -> str | None:
    return target.profiling_window or settings.profiling.window


def default_profiler(target: TargetConfig, settings: Settings):
    from gaia.dialects.bootstrap import registry  # lazy: imports only this dialect's driver

    return registry.profiler(target.dialect, target=target, budget=settings.profiling.budget)


def _Lock(session: AsyncSession, data_source_id: uuid.UUID) -> DataSourceLock:  # noqa: N802
    """One profiling run per data source (D33), the key profiling has always used."""
    return DataSourceLock(
        session, data_source_id, "profiling", error=ProfilingError,
        message="another profiling run holds the lock for this data source",
    )


class _TargetThread:
    """All target work on one thread, with one session opened on that thread."""

    def __init__(self, profiler) -> None:
        self.profiler = profiler
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gaia-profile")
        self.cm = None
        self.cur = None

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, lambda: fn(*args, **kwargs))

    async def __aenter__(self) -> _TargetThread:
        self.cm = self.profiler.session()
        self.cur = await self.call(self.cm.__enter__)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.call(self.cm.__exit__, None, None, None)
        finally:
            self.executor.shutdown(wait=True)


# -- shared loading --------------------------------------------------------------------------


@dataclass
class _Context:
    label: str
    target: TargetConfig
    data_source_id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot: Snapshot
    scope: list[TableSnapshot]
    table_ids: dict[str, uuid.UUID]  # qualified name -> catalog id


async def _context(
    settings: Settings,
    session: AsyncSession,
    options: ProfileOptions,
    snapshot_loader: Callable[[dict], Snapshot] | None,
) -> _Context:
    label = options.target or settings.app.default_target
    target = _target(settings, label)
    if target is None:
        raise ProfilingError(f"no target {label!r} in config, and no data source of that "
                             "name (make datasource ARGS=list)")
    if snapshot_loader is None:
        from gaia.introspect.service import snapshot_from_document as snapshot_loader
    store = SemanticsStore(session)
    data_source = await store.data_source(label)
    if data_source is None:
        raise ProfilingError(f"no data source {label!r} in the metadata store; run introspect")
    record = await store.latest_snapshot(data_source.id)
    if record is None:
        raise ProfilingError(f"no snapshot for {label!r}; run introspect")
    snapshot = snapshot_loader(record.document)
    live = await store.live_tables(data_source.id)
    scope = [
        t
        for t in select_tables(
            snapshot,
            schemas=options.schemas,
            tables=options.tables,
            with_neighbours=options.with_neighbours,
            limit=options.limit,
        )
        if (t.schema, t.name) in live
    ]
    ids = {f"{s}.{n}": r.id for (s, n), r in live.items()}
    return _Context(label, target, data_source.id, record.id, snapshot, scope, ids)


def _catalog_schema(snapshot: Snapshot) -> dict[str, dict[str, dict[str, str]]]:
    schema: dict[str, dict[str, dict[str, str]]] = {}
    for t in snapshot.tables:
        schema.setdefault(t.schema, {})[t.name] = {c.name: c.data_type for c in t.columns}
    return schema


# -- Stage 1 ---------------------------------------------------------------------------------


async def run_profiling(
    settings: Settings,
    session: AsyncSession,
    options: ProfileOptions,
    *,
    profiler_factory: Callable[[TargetConfig, Settings], Any] | None = None,
    snapshot_loader: Callable[[dict], Snapshot] | None = None,
    now: datetime | None = None,
) -> ProfileReport:
    started = time.perf_counter()
    ctx = await _context(settings, session, options, snapshot_loader)
    report = ProfileReport(ctx.label, ctx.snapshot.dialect, 1, options.dry_run)
    if not profiling_enabled(settings, ctx.label):
        raise ProfilingError(
            f"profiling is disabled for {ctx.label!r} (profiling.enabled / "
            "targets.<name>.profiling_enabled): reasoning uses catalog evidence only"
        )
    window = _window(settings, ctx.target)
    if not in_window(window, now) and not options.ignore_window:
        raise ProfilingError(f"outside the profiling window {window} (use --ignore-window)")

    cfg = settings.profiling
    store = ProfileStore(session)
    current = await store.current_profiles(ctx.table_ids[t.qualified_name] for t in ctx.scope)
    dialect = ctx.snapshot.dialect

    # Decide without touching the target.
    plan: list[tuple[TableSnapshot, str, str]] = []  # table, decision, detail
    for table in ctx.scope:
        table_id = ctx.table_ids[table.qualified_name]
        prof = current.get(table_id)
        if table.kind != "table":
            plan.append((table, "skip", "view"))
            continue
        if not options.force and prof is not None:
            fresh, why = freshness(
                profile_structure_hash=prof.structure_hash,
                profile_rows=prof.rows_at_profile,
                profiled_at=prof.profiled_at,
                status=prof.status,
                table=table,
                row_count_delta_ratio=cfg.row_count_delta_ratio,
                max_age_days=cfg.profile_max_age_days,
                now=now,
            )
            if fresh:
                plan.append((table, "reuse", why))
                continue
            if (
                prof.status == "skipped"
                and prof.skip_reason == "catalog_sufficient"
                and (prof.structure_hash == table.structure_hash)
            ):
                plan.append((table, "reuse", "catalog-sufficient, structure unchanged"))
                continue
        if catalog_sufficient(table, dialect):
            plan.append((table, "skip", "catalog_sufficient"))
            continue
        why = "new" if prof is None else "forced" if options.force else "stale"
        plan.append((table, "profile", why))

    if options.dry_run:
        for table, decision, detail in plan:
            action = {"skip": "skipped", "reuse": "reused", "profile": "would_profile"}[decision]
            report.results.append(
                TableAction(
                    table.qualified_name,
                    action,
                    detail,
                    pages=table_pages(table) if decision == "profile" else 0,
                )
            )
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        return report

    profiler = (profiler_factory or default_profiler)(ctx.target, settings)
    async with _Lock(session, ctx.data_source_id):
        run = await store.create_run(
            data_source_id=ctx.data_source_id,
            snapshot_id=ctx.snapshot_id,
            stage="1",
            status="running",
            options=_options_dict(options),
            tables_in_scope=len(ctx.scope),
        )
        run_id = run.id
        report.run_id = str(run_id)
        await session.commit()
        try:
            await _stage1(
                settings, session, store, ctx, plan, profiler, report, run_id, options, now
            )
        except BaseException as exc:
            await session.rollback()
            failed = await session.get(ProfileRun, run_id)
            if failed is not None:
                failed.status = "failed"
                failed.finished_at = datetime.now(UTC)
                failed.notes = [*report.notes, f"aborted: {type(exc).__name__}: {exc}"]
                await session.commit()
            raise

    report.wall_ms = int((time.perf_counter() - started) * 1000)
    run = await session.get(ProfileRun, run_id)
    assert run is not None
    failed = report.count("failed")
    run.status = "completed" if failed == 0 else "completed_with_failures"
    run.finished_at = datetime.now(UTC)
    run.tables_profiled = report.count("profiled") + report.count("partial")
    run.tables_reused = report.count("reused")
    run.tables_skipped = report.count("skipped")
    run.tables_failed = failed
    run.queries = report.queries
    run.pages_estimated = report.pages
    run.wall_ms = report.wall_ms
    run.footprint = report.footprint
    run.notes = report.notes
    await session.commit()
    return report


def _options_dict(options: ProfileOptions) -> dict[str, Any]:
    return {
        "schemas": options.schemas,
        "tables": options.tables,
        "with_neighbours": options.with_neighbours,
        "limit": options.limit,
        "force": options.force,
        "ignore_window": options.ignore_window,
        "joins": options.joins,
    }


async def _stage1(
    settings: Settings,
    session: AsyncSession,
    store: ProfileStore,
    ctx: _Context,
    plan: list[tuple[TableSnapshot, str, str]],
    profiler,
    report: ProfileReport,
    run_id: uuid.UUID,
    options: ProfileOptions,
    now: datetime | None,
) -> None:
    cfg = settings.profiling
    budget_cfg = cfg.budget
    window = _window(settings, ctx.target)
    run_budget = RunBudget(budget_cfg.max_pages_per_run)
    dialect = ctx.snapshot.dialect
    column_ids = await store.column_ids(ctx.table_ids[t.qualified_name] for t, _, _ in plan)
    current = await store.current_profiles(ctx.table_ids[t.qualified_name] for t, _, _ in plan)
    fresh: dict[str, TableProfileResult] = {}

    async with _TargetThread(profiler) as worker:
        cur = worker.cur
        total_start = profiler.queries
        before = await worker.call(profiler.stats_inventory, cur)

        # -- modules and view lineage (catalog reads) ---------------------------------------
        try:
            modules = await worker.call(profiler.modules, cur)
        except Exception as exc:  # no VIEW DEFINITION, etc.: lineage stays unknown
            modules = []
            report.notes.append(f"module definitions not readable: {type(exc).__name__}: {exc}")
        stored, report.modules = await store.sync_modules(ctx.data_source_id, modules)
        schema = _catalog_schema(ctx.snapshot)
        counts = {"pass_through": 0, "computed": 0, "unresolved": 0}
        for view in (t for t in ctx.snapshot.tables if t.kind == "view"):
            module = stored.get((view.schema, view.name))
            if module is None:
                continue
            lineage = parse_view_lineage(
                view.qualified_name,
                [c.name for c in sorted(view.columns, key=lambda c: c.ordinal)],
                module.definition,
                dialect,
                schema,
            )
            await store.replace_lineage(ctx.data_source_id, module, lineage)
            for item in lineage:
                counts[item.classification] += 1
        report.lineage = counts
        await session.commit()

        # -- tables ---------------------------------------------------------------------------
        for table, decision, detail in plan:
            table_id = ctx.table_ids[table.qualified_name]
            prof = current.get(table_id)
            structure_changed = prof is not None and prof.structure_hash != table.structure_hash
            if decision == "reuse":
                report.results.append(TableAction(table.qualified_name, "reused", detail))
                continue
            if decision == "skip":
                await _write_skip(
                    store, ctx, table, table_id, run_id, detail, prof, structure_changed
                )
                report.results.append(TableAction(table.qualified_name, "skipped", detail))
                await session.commit()
                continue
            if window and not options.ignore_window and not in_window(window, now):
                # The window closed during the run: the rest waits for the next one.
                await _write_skip(
                    store, ctx, table, table_id, run_id, "window", prof, structure_changed
                )
                report.results.append(TableAction(table.qualified_name, "skipped", "window"))
                await session.commit()
                continue
            try:
                allowed = await worker.call(profiler.can_select, cur, table.schema, table.name)
                reason = "no_select"
            except Exception as exc:  # not proof of a missing grant: try again next run
                allowed, reason = False, "privilege_check_failed"
                report.notes.append(f"{table.qualified_name}: privilege check failed: {exc}")
            if not allowed:
                await _write_skip(
                    store, ctx, table, table_id, run_id, reason, prof, structure_changed
                )
                report.results.append(TableAction(table.qualified_name, "skipped", reason))
                await session.commit()
                continue
            budget = TableBudget(
                budget_cfg.max_pages_per_table,
                budget_cfg.max_runtime_per_table_s,
                run_budget,
                statement_timeout_s=budget_cfg.statement_timeout_s,
                time_rule=budget_cfg.time_rule,
            )
            if run_budget.remaining() <= 0:
                await _write_skip(
                    store, ctx, table, table_id, run_id, "budget", prof, structure_changed
                )
                report.results.append(TableAction(table.qualified_name, "skipped", "budget"))
                await session.commit()
                continue
            try:
                result = await worker.call(
                    profile_table,
                    profiler,
                    cur,
                    table,
                    dialect=dialect,
                    cfg=budget_cfg,
                    budget=budget,
                    seed=cfg.seed,
                )
            except Exception as exc:  # a bug or a lost connection: record, go on
                logger.warning("profiling failed for %s: %s", table.qualified_name, exc)
                result = TableProfileResult(
                    table.qualified_name,
                    "failed",
                    "unknown",
                    None,
                    notes=[f"{type(exc).__name__}: {exc}"],
                )
            await _write_profile(
                store,
                ctx,
                table,
                table_id,
                run_id,
                result,
                profiler,
                column_ids.get(table_id, {}),
                structure_changed,
            )
            await session.commit()
            if result.status != "failed":
                fresh[table.qualified_name] = result
            report.results.append(
                TableAction(
                    table.qualified_name,
                    result.status,
                    "; ".join(result.notes[:2]),
                    queries=result.queries,
                    pages=result.pages,
                    runtime_ms=result.runtime_ms,
                    size_class=result.size_class,
                    notes=list(result.notes),
                )
            )

        # -- join discovery -------------------------------------------------------------------
        if fresh and options.joins:
            await _discover_joins(
                session,
                store,
                ctx,
                fresh,
                profiler,
                worker,
                report,
                column_ids,
                run_budget,
                budget_cfg.max_join_tests,
                cfg=cfg,
            )

        after = await worker.call(profiler.stats_inventory, cur)
        # Data queries: statements that read table rows (profiles, join tests);
        # the rest read the catalog (modules, privileges, statistics metadata).
        report.queries = (
            sum(r.queries for r in report.results) + len(report.joins) + report.join_samples
        )
        report.catalog_queries = profiler.queries - total_start - report.queries
    report.pages = run_budget.pages
    if before is not None and after is not None:
        added = [after[k] for k in sorted(set(after) - set(before))]
        report.footprint = {
            "statistics_before": len(before),
            "statistics_after": len(after),
            "new": added,
            "note": (
                "auto-created by the engine as a side effect of Gaia's SELECTs (D4′)"
                if dialect == "mssql"
                else "PostgreSQL creates statistics only by ANALYZE; anything here is "
                "autovacuum running concurrently, not Gaia"
            ),
        }


async def _write_skip(
    store: ProfileStore,
    ctx: _Context,
    table: TableSnapshot,
    table_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: str,
    prof,
    structure_changed: bool,
) -> None:
    if (
        prof is not None
        and prof.status == "skipped"
        and prof.skip_reason == reason
        and (not structure_changed)
    ):
        return  # the same verdict is already current
    await store.write_table_profile(
        data_source_id=ctx.data_source_id,
        table_id=table_id,
        run_id=run_id,
        structure_hash=table.structure_hash,
        status="skipped",
        skip_reason=reason,
        columns={},
        structure_changed=structure_changed,
        rows_at_profile=table.estimated_rows,
    )


async def _write_profile(
    store: ProfileStore,
    ctx: _Context,
    table: TableSnapshot,
    table_id: uuid.UUID,
    run_id: uuid.UUID,
    result: TableProfileResult,
    profiler,
    column_ids: dict[str, uuid.UUID],
    structure_changed: bool,
) -> None:
    isolation = getattr(profiler, "isolation", None)
    columns = {}
    for name, summary in result.columns.items():
        metrics = result.metrics.get(name, {})
        for entry in metrics.values():
            entry.setdefault("isolation", isolation)
            if entry.get("method") == "sampled_pages":
                entry.setdefault("seed", result.seed)
        columns[name] = (column_ids.get(name), summary, metrics)
    table_metrics = result.metrics.get("*", {})
    evidence = {
        "identities": [
            {
                "expression": i.expression,
                "columns": list(i.columns),
                "holds_rate": i.holds_rate,
                "kind": i.kind,
            }
            for i in result.identities
        ],
        "metrics": table_metrics,
        "sample_rows": result.sample_rows,
    }
    await store.write_table_profile(
        data_source_id=ctx.data_source_id,
        table_id=table_id,
        run_id=run_id,
        structure_hash=table.structure_hash,
        status=result.status,
        skip_reason=None,
        columns=columns,
        structure_changed=structure_changed,
        rows_at_profile=table.estimated_rows,
        row_count=result.row_count,
        size_class=result.size_class,
        pages_estimated=result.pages,
        runtime_ms=result.runtime_ms,
        queries=result.queries,
        isolation=isolation,
        seed=result.seed,
        evidence=evidence,
        notes=result.notes,
    )


async def _discover_joins(
    session: AsyncSession,
    store: ProfileStore,
    ctx: _Context,
    fresh: dict[str, TableProfileResult],
    profiler,
    worker: _TargetThread,
    report: ProfileReport,
    column_ids: dict[uuid.UUID, dict[str, uuid.UUID]],
    run_budget: RunBudget,
    max_tests: int,
    *,
    cfg=None,
) -> None:
    """Stage 1 join candidates among profiled tables (parents: any current profile).

    Children are the tables profiled in this run, and (D12) the tables profiled
    in earlier runs against the parents profiled in this one: sample values are
    never stored, so their values are drawn again, for those tables only."""
    summaries: dict[str, dict[str, ColumnProfileSummary]] = {}
    evidence = await store.load_evidence(ctx.data_source_id, ctx.snapshot, enabled=True)
    for t in ctx.snapshot.tables:
        ev: TableEvidenceInput | None = evidence.table_evidence(t.qualified_name)
        if ev is not None and ev.columns:
            summaries[t.qualified_name] = ev.columns
    for name, result in fresh.items():
        summaries[name] = result.columns
    child_values = {
        (name, col): values for name, r in fresh.items() for col, values in r.join_values.items()
    }
    parent_values = dict(child_values)
    candidates = find_candidates(ctx.snapshot, summaries, child_values, parent_values)
    types = {
        (t.qualified_name, c.name): c.data_type for t in ctx.snapshot.tables for c in t.columns
    }
    if cfg is not None:
        extra, extra_values = await _reused_children(
            store, ctx, fresh, summaries, parent_values, profiler, worker, report, run_budget,
            cfg,
        )
        candidates += extra
        child_values.update(extra_values)
        missing = {ctx.table_ids[c.child_table] for c in extra} - set(column_ids)
        if missing:
            column_ids.update(await store.column_ids(missing))
    if len(candidates) > max_tests:
        report.notes.append(
            f"join candidates: {len(candidates)} found, {max_tests} tested "
            "(profiling.budget.max_join_tests)"
        )
    tables = {t.qualified_name: t for t in ctx.snapshot.tables}
    for cand in candidates[:max_tests]:
        parent = tables[cand.parent_table]
        cost = max(
            1,
            min(table_pages(parent), 4 * len(child_values[(cand.child_table, cand.child_column)])),
        )
        if run_budget.pages + cost > run_budget.max_pages:
            report.notes.append("join tests stopped: run page budget used up")
            break
        started = time.perf_counter()
        try:
            outcome = await worker.call(
                inclusion_test,
                profiler,
                worker.cur,
                cand.parent_table,
                cand.parent_column,
                child_values[(cand.child_table, cand.child_column)],
                stage=1,
                origin="candidate",
                specific=cand.specific,
                parent_type=types.get((cand.parent_table, cand.parent_column)),
                child_type=types.get((cand.child_table, cand.child_column)),
                density=integer_density(
                    summaries.get(cand.parent_table, {}).get(cand.parent_column)
                ),
            )
        except Exception as exc:
            report.notes.append(
                f"inclusion test {cand.child_table}.{cand.child_column} -> "
                f"{cand.parent_table}.{cand.parent_column} failed: {exc}"
            )
            continue
        run_budget.pages += cost
        runtime = int((time.perf_counter() - started) * 1000)
        result = outcome.result
        entry = {
            "child": f"{cand.child_table}.{cand.child_column}",
            "parent": f"{cand.parent_table}.{cand.parent_column}",
            "rate": result.metric,
            "passed": result.passed,
            "specific": result.specific,  # decided by the outcome (joins.chance_of_hits)
            "reason": cand.reason,
            "distinct_rate": round(outcome.distinct_rate, 4),
            "neighbours": outcome.neighbour_rate,  # D54 control (integer tests)
            "kept": outcome.worth_keeping,
        }
        report.joins.append(entry)
        if not outcome.worth_keeping:
            continue  # says nothing useful about the column; not kept as evidence
        table_id = ctx.table_ids[cand.child_table]
        await store.write_test(
            data_source_id=ctx.data_source_id,
            table_id=table_id,
            column_id=column_ids.get(table_id, {}).get(cand.child_column),
            column_name=cand.child_column,
            result=result,
            stage=1,
            target=f"{cand.parent_table}.{cand.parent_column}",
            method="sampled_values",
            runtime_ms=runtime,
        )
        await session.commit()


async def _reused_children(
    store: ProfileStore,
    ctx: _Context,
    fresh: dict[str, TableProfileResult],
    summaries: dict[str, dict[str, ColumnProfileSummary]],
    parent_values: dict[tuple[str, str], Any],
    profiler,
    worker: _TargetThread,
    report: ProfileReport,
    run_budget: RunBudget,
    cfg,
) -> tuple[list, dict[tuple[str, str], Any]]:
    """Candidates from tables profiled in earlier runs to parents profiled now."""
    tables = {t.qualified_name: t for t in ctx.snapshot.tables if t.kind == "table"}
    new_parents = {name for name in fresh if name in tables and single_keys(tables[name])}
    reused = [
        name for name in summaries if name in tables and name not in fresh
    ]
    if not new_parents or not reused:
        return [], {}
    # 1. a cheap pre-filter from what the stored profiles already say
    stand_in = {
        (name, col): [(v, int(n)) for v, n in (s.top_values or [])]
        + [(v, 1) for v in s.sample_values or []]
        for name in reused
        for col, s in summaries[name].items()
    }
    stand_in = {k: v for k, v in stand_in.items() if v}
    maybe = find_candidates(
        ctx.snapshot, summaries, stand_in, parent_values, parents_in=new_parents
    )
    children = {c.child_table for c in maybe if c.child_table in ctx.table_ids}
    existing = await store.current_tests(ctx.table_ids[n] for n in children)
    done = {
        (name, t.column_name, t.test_id)
        for name in children
        for t in existing.get(ctx.table_ids[name], [])
    }
    wanted: dict[str, set[str]] = {}
    for cand in maybe:
        key = (cand.child_table, cand.child_column, f"inc:{cand.parent_table}.{cand.parent_column}")
        if key not in done:
            wanted.setdefault(cand.child_table, set()).add(cand.child_column)
    if not wanted:
        return [], {}
    # 2. real values, one sample statement per table
    budget = cfg.budget
    values: dict[tuple[str, str], Any] = {}
    for name in sorted(wanted):
        table = tables[name]
        cols = [(c.name, c.data_type) for c in table.columns if c.name in wanted[name]]
        exprs = [(n, profiler.sample_expr(n, dt)) for n, dt in cols]
        exprs = [(n, e) for n, e in exprs if e is not None]
        if not exprs:
            continue
        whole = (table.estimated_rows or 0) <= budget.sample_target_rows
        cost = _sample_cost(table, budget)
        if run_budget.pages + cost > run_budget.max_pages:
            report.notes.append("join candidates of earlier-profiled tables: run page budget")
            break
        sql = profiler.sample_sql(
            profiler.table(table.schema, table.name), [e for _n, e in exprs],
            budget.sample_target_rows, table.estimated_rows, cfg.seed, whole,
        )
        try:
            rows = await worker.call(profiler.query, worker.cur, sql, None,
                                     budget.max_sample_rows)
        except Exception as exc:
            report.notes.append(f"{name}: join sample failed: {type(exc).__name__}: "
                                f"{scrub(str(exc))}")
            continue
        run_budget.pages += cost
        report.join_samples += 1
        for i, (col, _e) in enumerate(exprs):
            column = [r[i] for r in rows]
            column = [None if isinstance(v, str) and not v.strip() else v for v in column]
            if any(v is not None for v in column):
                values[(name, col)] = join_sample(column, budget.join_test_values, cfg.seed)
    found = [
        c for c in find_candidates(ctx.snapshot, summaries, values, parent_values,
                                   parents_in=new_parents)
        if (c.child_table, c.child_column, f"inc:{c.parent_table}.{c.parent_column}") not in done
    ]
    if found:
        report.notes.append(
            f"join candidates: {len(wanted)} earlier-profiled table(s) re-sampled against "
            f"{len(new_parents)} newly profiled parent(s)"
        )
    return found, values


# -- Stage 3 ---------------------------------------------------------------------------------


async def run_stage3(
    settings: Settings,
    session: AsyncSession,
    options: ProfileOptions,
    *,
    profiler_factory: Callable[[TargetConfig, Settings], Any] | None = None,
    snapshot_loader: Callable[[dict], Snapshot] | None = None,
    now: datetime | None = None,
) -> ProfileReport:
    """Run the tests that apply to the stored claims and have not run yet."""
    started = time.perf_counter()
    ctx = await _context(settings, session, options, snapshot_loader)
    report = ProfileReport(ctx.label, ctx.snapshot.dialect, 3, options.dry_run)
    if not profiling_enabled(settings, ctx.label):
        raise ProfilingError(f"profiling is disabled for {ctx.label!r}")
    window = _window(settings, ctx.target)
    if not in_window(window, now) and not options.ignore_window:
        raise ProfilingError(f"outside the profiling window {window} (use --ignore-window)")

    store = ProfileStore(session)
    evidence = await store.load_evidence(ctx.data_source_id, ctx.snapshot, enabled=True)
    scope = [t for t in ctx.scope if t.kind == "table"]
    ids = [ctx.table_ids[t.qualified_name] for t in scope]
    roles: dict[tuple[Any, str], str] = {}
    if ids:
        rows = await session.execute(
            select(ColumnSemantic.table_id, ColumnSemantic.column_id, ColumnSemantic.role).where(
                ColumnSemantic.table_id.in_(ids), ColumnSemantic.is_current.is_(True)
            )
        )
        names = await store.column_ids(ids)
        by_column_id = {cid: (tid, n) for tid, cols in names.items() for n, cid in cols.items()}
        for table_id, column_id, role in rows:
            if column_id in by_column_id:
                roles[(table_id, by_column_id[column_id][1])] = role
    hints: dict[tuple[str, str], list[str]] = {}
    skipped_composite = 0
    statuses = ("inferred", "refuted") if options.force else ("inferred",)
    for h in await session.scalars(
        select(JoinHint).where(
            JoinHint.data_source_id == ctx.data_source_id,
            JoinHint.source == "llm",
            JoinHint.status.in_(statuses),
        )
    ):
        if len(h.from_columns) != 1 or len(h.to_columns) != 1:
            skipped_composite += 1
            continue
        hints.setdefault((f"{h.from_schema}.{h.from_table}", h.from_columns[0]), []).append(
            f"{h.to_schema}.{h.to_table}.{h.to_columns[0]}"
        )
    if skipped_composite:
        report.notes.append(
            f"{skipped_composite} composite join hint(s) not tested (single-column inclusion only)"
        )

    existing = await store.current_tests(ids)
    work: list[tuple[TableSnapshot, str, str, str]] = []  # table, column, test key, role
    for table in scope:
        table_id = ctx.table_ids[table.qualified_name]
        ev = evidence.table_evidence(table.qualified_name)
        bundle = build_bundle(table, dialect=ctx.snapshot.dialect)
        if options.force and ev is not None:  # re-test: ignore earlier claim tests
            ev = TableEvidenceInput(
                table=ev.table,
                profiling_available=ev.profiling_available,
                columns=ev.columns,
                identities=ev.identities,
                aliases=ev.aliases,
                tests={},
                lineage=ev.lineage,
            )
        for name, facts in bundle.facts.items():
            role = roles.get((table_id, name))
            targets = hints.get((table.qualified_name, name), [])
            if role is None and not targets:
                continue
            for key in pending_claim_tests(role or "", facts, ev, targets):
                work.append((table, name, key, role or ""))
    if options.dry_run:
        for table, column, key, role in work:
            report.results.append(
                TableAction(
                    f"{table.qualified_name}.{column}",
                    "would_test",
                    f"{key} (claimed {role or 'join hint'})",
                )
            )
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        return report

    profiler = (profiler_factory or default_profiler)(ctx.target, settings)
    run_budget = RunBudget(settings.profiling.budget.max_pages_per_run)
    types = {
        (t.qualified_name, c.name): c.data_type for t in ctx.snapshot.tables for c in t.columns
    }
    tables = {t.qualified_name: t for t in ctx.snapshot.tables}
    async with _Lock(session, ctx.data_source_id):
        run = await store.create_run(
            data_source_id=ctx.data_source_id,
            snapshot_id=ctx.snapshot_id,
            stage="3",
            status="running",
            options=_options_dict(options),
            tables_in_scope=len(scope),
        )
        run_id = run.id
        report.run_id = str(run_id)
        await session.commit()
        try:
            await _stage3_tests(
                settings,
                session,
                store,
                ctx,
                work,
                existing,
                evidence,
                profiler,
                report,
                run_budget,
                types,
                tables,
                options,
            )
        except BaseException as exc:
            await session.rollback()
            failed = await session.get(ProfileRun, run_id)
            if failed is not None:
                failed.status = "failed"
                failed.finished_at = datetime.now(UTC)
                failed.notes = [*report.notes, f"aborted: {type(exc).__name__}: {exc}"]
                await session.commit()
            raise
        await store.mark_stage3(ids)
        report.pages = run_budget.pages
        report.wall_ms = int((time.perf_counter() - started) * 1000)
        run = await session.get(ProfileRun, run_id)
        assert run is not None
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.queries = report.queries
        run.pages_estimated = report.pages
        run.wall_ms = report.wall_ms
        run.notes = report.notes
        await session.commit()
    for qualified in sorted({t.qualified_name for t, _c, _k, _r in work}):
        n = sum(1 for e in report.tests if e["column"].startswith(qualified + "."))
        report.results.append(TableAction(qualified, "tested", f"{n} test(s)"))
    return report


async def _stage3_tests(
    settings: Settings,
    session: AsyncSession,
    store: ProfileStore,
    ctx: _Context,
    work: list[tuple[TableSnapshot, str, str, str]],
    existing: dict[uuid.UUID, list[Any]],
    evidence,
    profiler,
    report: ProfileReport,
    run_budget: RunBudget,
    types: dict[tuple[str, str], str],
    tables: dict[str, TableSnapshot],
    options: ProfileOptions,
) -> None:
    cfg = settings.profiling
    budget_cfg = cfg.budget
    dialect = ctx.snapshot.dialect
    budgets: dict[str, TableBudget] = {}
    async with _TargetThread(profiler) as worker:
        cur = worker.cur
        queries_start = profiler.queries
        for table, column, key, role in work:
            table_id = ctx.table_ids[table.qualified_name]
            column_id = (await store.column_ids([table_id]))[table_id].get(column)
            budget = budgets.setdefault(
                table.qualified_name,
                TableBudget(
                    budget_cfg.max_pages_per_table,
                    budget_cfg.max_runtime_per_table_s,
                    run_budget,
                    statement_timeout_s=budget_cfg.statement_timeout_s,
                    time_rule=budget_cfg.time_rule,
                ),
            )
            kind, _, target = key.partition(":")
            started_test = time.perf_counter()
            label = f"{table.qualified_name}.{column}"

            async def not_run(reason: str, *, _key: str = key, _kind: str = kind,
                              _label: str = label, _table_id=table_id, _column_id=column_id,
                              _column: str = column, _target: str = target,
                              _started: float = started_test) -> None:
                """A claim test that cannot run is recorded as inconclusive, so the
                column is not escalated forever (it goes to the SME instead); a
                later ``--force`` run retries it."""
                report.notes.append(f"{_label} {_key}: not run ({reason})")
                result = VerificationResult(
                    test_id=_key,
                    kind=_TEST_KINDS.get(_kind, _kind),
                    passed=False,
                    metric=None,
                    detail=f"not run: {reason}",
                    origin="claim",
                    conclusive=False,
                )
                await store.write_test(
                    data_source_id=ctx.data_source_id,
                    table_id=_table_id,
                    column_id=_column_id,
                    column_name=_column,
                    result=result,
                    stage=3,
                    target=_target if _kind == "inc" else None,
                    method="not_run",
                    runtime_ms=int((time.perf_counter() - _started) * 1000),
                )
                await session.commit()
                report.tests.append(_test_entry(_label, _key, False, None, "not run"))

            try:
                if kind == "inc":
                    adopted = next(
                        (
                            t
                            for t in existing.get(table_id, [])
                            if t.column_name == column
                            and t.test_id == key
                            and t.origin == "candidate"
                        ),
                        None,
                    )
                    if adopted is not None and not options.force:
                        row = await store.adopt_test(adopted)
                        report.hints[await _hint_outcome(
                            session, ctx, table, column, target, row.passed, row.metric
                        )] += 1
                        report.tests.append(
                            _test_entry(label, row.test_id, row.passed, row.metric, "adopted")
                        )
                        await session.commit()
                        continue
                    pt, _, pc = target.rpartition(".")
                    parent_table = tables.get(pt)
                    if parent_table is None or pc not in {c.name for c in parent_table.columns}:
                        await not_run(f"{target} is not in the current catalog")
                        continue
                    expr = profiler.sample_expr(column, types.get((table.qualified_name,
                                                                   column), ""))
                    if expr is None:
                        await not_run("the column's type cannot be sampled")
                        continue
                    # An index seek per value on a key; a scan otherwise.
                    seek = pc in single_keys(parent_table)
                    cost = _sample_cost(table, budget_cfg) + (
                        min(table_pages(parent_table), 4 * budget_cfg.join_test_values)
                        if seek
                        else table_pages(parent_table)
                    )
                    refusal = budget.refusal(cost)
                    if refusal:
                        if refusal.startswith("run page budget"):
                            report.notes.append(f"{label} {key}: not run ({refusal})")
                        else:
                            await not_run(refusal)
                        continue
                    # An unbiased set of values (3A correction 3, now in Stage 3 too):
                    # rows of the sample in seeded random order, not the most
                    # frequent values, which over-represent heavy hitters.
                    whole = _whole(table, budget_cfg)
                    sql = profiler.sample_sql(
                        profiler.table(table.schema, table.name),
                        [expr],
                        budget_cfg.sample_target_rows,
                        table.estimated_rows,
                        cfg.seed,
                        whole,
                    )
                    rows = await worker.call(
                        profiler.query, cur, sql, None, budget_cfg.max_sample_rows
                    )
                    weighted = join_sample(
                        [r[0] for r in rows], budget_cfg.join_test_values, cfg.seed
                    )
                    density = None
                    if family(dialect, types.get((pt, pc), "")) == "integer":
                        parent_ev = evidence.table_evidence(pt)
                        density = integer_density(
                            parent_ev.columns.get(pc) if parent_ev else None
                        )
                    outcome = await worker.call(
                        inclusion_test,
                        profiler,
                        cur,
                        pt,
                        pc,
                        weighted,
                        stage=3,
                        origin="claim",
                        parent_type=types.get((pt, pc)),
                        child_type=types.get((table.qualified_name, column)),
                        density=density,
                    )
                    result = outcome.result
                    method = "exact" if whole else "sampled_pages"
                elif kind == "uniq":
                    cost = 2 * column_pages(table, table_pages(table),
                                             profiler.is_columnstore(table))
                    refusal = budget.refusal(cost)
                    if refusal:
                        if refusal.startswith("run page budget"):
                            report.notes.append(f"{label} {key}: not run ({refusal})")
                        else:
                            await not_run(refusal)
                        continue
                    result = await worker.call(uniqueness_test, profiler, cur, table, column)
                    method, target = "exact", None
                elif kind == "topk":
                    cost = column_pages(table, table_pages(table), profiler.is_columnstore(table))
                    refusal = budget.refusal(cost)
                    if refusal:
                        if refusal.startswith("run page budget"):
                            report.notes.append(f"{label} {key}: not run ({refusal})")
                        else:
                            await not_run(refusal)
                        continue
                    result, _top = await worker.call(
                        topk_test,
                        profiler,
                        cur,
                        table,
                        column,
                        role,
                        budget_cfg.low_cardinality_max,
                    )
                    method, target = "exact", None
                else:
                    continue
            except Exception as exc:
                await not_run(f"{type(exc).__name__}: {scrub(str(exc))}")
                continue
            budget.spend(cost)
            budget.observe(time.perf_counter() - started_test)
            await store.write_test(
                data_source_id=ctx.data_source_id,
                table_id=table_id,
                column_id=column_id,
                column_name=column,
                result=result,
                stage=3,
                target=target or None,
                method=method,
                runtime_ms=int((time.perf_counter() - started_test) * 1000),
            )
            if kind == "inc":
                report.hints[await _hint_outcome(
                    session, ctx, table, column, target, result.passed, result.metric
                )] += 1
            await session.commit()
            report.tests.append(
                _test_entry(label, result.test_id, result.passed, result.metric, method)
            )
        report.queries = profiler.queries - queries_start


def _whole(table: TableSnapshot, budget_cfg) -> bool:
    return (table.estimated_rows or 0) <= budget_cfg.sample_target_rows


def _sample_cost(table: TableSnapshot, budget_cfg) -> int:
    pages = table_pages(table)
    if _whole(table, budget_cfg):
        return pages
    rows_per_page = max(1.0, (table.estimated_rows or 1) / pages)
    return min(pages, math.ceil(budget_cfg.sample_target_rows / rows_per_page) * 2)


def _test_entry(
    column: str, test_id: str, passed: bool, metric: float | None, method: str
) -> dict[str, Any]:
    return {"column": column, "test": test_id, "passed": passed, "metric": metric, "method": method}


_TEST_KINDS = {"inc": "inclusion", "uniq": "uniqueness", "topk": "topk_exact"}
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def scrub(message: str, limit: int = 200) -> str:
    """A driver error without the data values it quotes (a conversion error
    echoes the offending value, which may be personal data); first line only."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    return _QUOTED.sub("'…'", first)[:limit]


async def _hint_outcome(
    session: AsyncSession,
    ctx: _Context,
    table: TableSnapshot,
    column: str,
    target: str,
    passed: bool,
    metric: float | None,
) -> str:
    """Feed a claim's inclusion test back into the model's join hint (D2):
    clearly failed (< 50 % of rows found) -> ``refuted``; passed -> ``inferred``
    again if it had been refuted. Between the two (orphans, a polymorphic
    column) the hint stays as it is, for the SME."""
    pt, _, pc = target.rpartition(".")
    to_schema, _, to_table = pt.partition(".")
    hints = await session.scalars(
        select(JoinHint).where(
            JoinHint.data_source_id == ctx.data_source_id,
            JoinHint.source == "llm",
            JoinHint.from_schema == table.schema,
            JoinHint.from_table == table.name,
            JoinHint.to_schema == to_schema,
            JoinHint.to_table == to_table,
        )
    )
    outcome = "unchanged"
    for hint in hints:
        if list(hint.from_columns) != [column] or list(hint.to_columns) != [pc]:
            continue
        if not passed and metric is not None and metric < KEEP_RATE:
            if hint.status == "inferred":
                hint.status = "refuted"
                outcome = "refuted"
        elif passed and hint.status == "refuted":
            hint.status = "inferred"
            outcome = "restored"
    await session.flush()
    return outcome


# -- evidence for reasoning ------------------------------------------------------------------


async def load_profile_evidence(
    settings: Settings,
    session: AsyncSession,
    label: str,
    data_source_id: uuid.UUID,
    snapshot: Snapshot,
):
    """The stored profiles, tests and lineage as the reasoning evidence source."""
    return await ProfileStore(session).load_evidence(
        data_source_id, snapshot, enabled=profiling_enabled(settings, label)
    )
