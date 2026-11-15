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
import hashlib
import logging
import math
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings, TargetConfig
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
    CHANCE_MAX,
    family,
    find_candidates,
    inclusion_test,
    integer_chance,
    single_keys,
    topk_test,
    uniqueness_test,
)
from gaia.profiling.lineage import parse_view_lineage
from gaia.profiling.statistics import TableProfileResult, profile_table
from gaia.profiling.summary import ColumnProfileSummary, TableEvidenceInput
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
    footprint: dict[str, Any] | None = None
    queries: int = 0  # data queries (read table rows)
    catalog_queries: int = 0
    pages: int = 0
    wall_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for r in self.results if r.action == action)


# -- configuration ---------------------------------------------------------------------------


def profiling_enabled(settings: Settings, label: str) -> bool:
    target = settings.targets.get(label)
    return bool(settings.profiling.enabled and (target is None or target.profiling_enabled))


def _window(settings: Settings, target: TargetConfig) -> str | None:
    return target.profiling_window or settings.profiling.window


def default_profiler(target: TargetConfig, settings: Settings):
    from gaia.dialects.bootstrap import registry  # lazy: imports only this dialect's driver

    return registry.profiler(target.dialect, target=target, budget=settings.profiling.budget)


def _lock_key(data_source_id: uuid.UUID) -> int:
    digest = hashlib.sha256(f"gaia-profiling:{data_source_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class _Lock:
    """Session-level advisory lock on a dedicated metadata connection."""

    def __init__(self, session: AsyncSession, data_source_id: uuid.UUID) -> None:
        self.engine = session.bind
        self.key = _lock_key(data_source_id)
        self.conn = None

    async def __aenter__(self) -> _Lock:
        self.conn = await self.engine.connect()
        got = await self.conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})
        await self.conn.commit()
        if not got:
            await self.conn.close()
            raise ProfilingError("another profiling run holds the lock for this data source")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            await self.conn.commit()
        finally:
            await self.conn.close()


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
    target = settings.targets.get(label)
    if target is None:
        raise ProfilingError(f"no target {label!r} in config")
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
            )

        after = await worker.call(profiler.stats_inventory, cur)
        # Data queries: statements that read table rows (profiles, join tests);
        # the rest read the catalog (modules, privileges, statistics metadata).
        report.queries = sum(r.queries for r in report.results) + len(report.joins)
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
) -> None:
    """Stage 1 join candidates among profiled tables (parents: any current profile)."""
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
            "specific": cand.specific,
            "reason": cand.reason,
            "distinct_rate": round(outcome.distinct_rate, 4),
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
    for h in await session.scalars(
        select(JoinHint).where(
            JoinHint.data_source_id == ctx.data_source_id,
            JoinHint.source == "llm",
            JoinHint.status == "inferred",
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
                ),
            )
            kind, _, target = key.partition(":")
            started_test = time.perf_counter()
            label = f"{table.qualified_name}.{column}"
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
                        report.tests.append(
                            _test_entry(label, row.test_id, row.passed, row.metric, "adopted")
                        )
                        await session.commit()
                        continue
                    pt, _, pc = target.rpartition(".")
                    parent_table = tables[pt]
                    # An index seek per value on a key; a scan otherwise.
                    seek = pc in single_keys(parent_table)
                    cost = _sample_cost(table, budget_cfg) + (
                        min(table_pages(parent_table), 4 * budget_cfg.join_test_values)
                        if seek
                        else table_pages(parent_table)
                    )
                    refusal = budget.refusal(cost)
                    if refusal:
                        report.notes.append(f"{label} {key}: not run ({refusal})")
                        continue
                    sql = profiler.values_sql(
                        profiler.table(table.schema, table.name),
                        column,
                        budget_cfg.join_test_values,
                        budget_cfg.sample_target_rows,
                        table.estimated_rows,
                        cfg.seed,
                        _whole(table, budget_cfg),
                    )
                    rows = await worker.call(profiler.query, cur, sql)
                    weighted = [(r[0], int(r[1])) for r in rows]
                    specific = True
                    if family(dialect, types.get((pt, pc), "")) == "integer":
                        parent_ev = evidence.table_evidence(pt)
                        parent = parent_ev.columns.get(pc) if parent_ev else None
                        specific = integer_chance(parent, len(weighted)) < CHANCE_MAX
                    outcome = await worker.call(
                        inclusion_test,
                        profiler,
                        cur,
                        pt,
                        pc,
                        weighted,
                        stage=3,
                        origin="claim",
                        specific=specific,
                        parent_type=types.get((pt, pc)),
                    )
                    result = outcome.result
                    method = "exact" if _whole(table, budget_cfg) else "sampled_pages"
                elif kind == "uniq":
                    cost = 2 * table_pages(table)
                    refusal = budget.refusal(cost)
                    if refusal:
                        report.notes.append(f"{label} {key}: not run ({refusal})")
                        continue
                    result = await worker.call(uniqueness_test, profiler, cur, table, column)
                    method, target = "exact", None
                elif kind == "topk":
                    cost = table_pages(table)
                    refusal = budget.refusal(cost)
                    if refusal:
                        report.notes.append(f"{label} {key}: not run ({refusal})")
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
                report.notes.append(f"{label} {key} failed: {type(exc).__name__}: {exc}")
                continue
            budget.spend(cost)
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
