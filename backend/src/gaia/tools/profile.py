"""Collect evidence from the target (Phase 3A): Stage 1 profile, Stage 3 tests.

Usage:
    python -m gaia.tools.profile --dry-run                   # the plan, no target reads
    python -m gaia.tools.profile --target demo_pg            # Stage 1: profile + joins
    python -m gaia.tools.profile --table legacy.tb_trx02 --with-neighbours
    python -m gaia.tools.profile --stage 3                   # tests of the stored claims
    python -m gaia.tools.profile --report                    # per-table / per-test detail

The recommended sequence for a data source:

    make introspect → make profile → make reason → make profile-tests → make reason

Stage 1 works from the latest stored snapshot. A table whose profile is still
fresh (same structure, row count within ``row_count_delta_ratio``, younger
than ``profile_max_age_days``) is reused without a single data query; ``--force``
re-profiles. Stage 3 runs only the tests that apply to the claims stored by the
last reasoning and have not run yet; the next ``make reason`` re-reasons exactly
the tables whose evidence changed.

Target safety: only ``SELECT``; read-only session; statement timeout; MAXDOP
(or ``max_parallel_workers_per_gather``); page and time budgets per table and
per run; one run per data source at a time (advisory lock). Outside the
configured window the tool refuses unless ``--ignore-window``.

Exit codes: 0 = done; 1 = refused or aborted; 2 = some tables failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.profiling.service import (
    ProfileOptions,
    ProfileReport,
    ProfilingError,
    run_profiling,
    run_stage3,
)


def _print_report(report: ProfileReport, *, detail: bool) -> None:
    print(f"data source    : {report.data_source} ({report.dialect})")
    print(
        f"stage          : {report.stage}"
        + ("  [dry run: no target reads]" if report.dry_run else "")
    )
    if report.run_id:
        print(f"run            : {report.run_id}")
    print()
    width = max((len(r.qualified_name) for r in report.results), default=10)
    for r in sorted(report.results, key=lambda x: x.qualified_name):
        line = f"  {r.qualified_name:<{width}}  {r.action:<13}"
        if r.size_class:
            line += f" {r.size_class:<11}"
        if r.queries:
            line += f" q={r.queries:<3} pages~{r.pages:<8,} {r.runtime_ms:>6} ms"
        elif r.pages:
            line += f" table pages~{r.pages:,}"
        if r.detail and not (detail and r.notes):
            line += f"  ({r.detail[:110]})"
        print(line)
        if detail or r.action == "partial":
            for note in r.notes:  # why a table is partial (D5)
                print(f"      - {note}")
    print()
    counts: dict[str, int] = {}
    for r in report.results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"tables         : {dict(sorted(counts.items()))}")
    if report.dry_run:
        return
    if report.modules:
        print(f"modules        : {report.modules}")
    if report.lineage:
        print(f"view lineage   : {report.lineage}")
    print(
        f"target         : data queries={report.queries} "
        f"catalog queries={report.catalog_queries} pages~{report.pages:,} "
        f"wall={report.wall_ms / 1000:.1f}s"
    )
    if report.joins:
        kept = [j for j in report.joins if j["kept"]]
        passed = [j for j in report.joins if j["passed"] and j["specific"]]
        print(
            f"join candidates: tested={len(report.joins)} kept={len(kept)} "
            f"passed(>=95% of rows)={len(passed)}"
        )
        rows = report.joins if detail else kept
        for j in sorted(rows, key=lambda j: (j["child"], -(j["rate"] or 0))):
            rate = "n/a" if j["rate"] is None else f"{j['rate']:.0%} rows"
            share = f"{j['distinct_rate']:.0%} values"
            flag = ("" if j["kept"] else "  (not kept)") + (
                "" if j["specific"] else "  (weak: dense integer key)"
            )
            print(f"  {j['child']:<38} -> {j['parent']:<38} {rate:>9} {share:>11}{flag}")
    if report.tests:
        print(f"tests          : {len(report.tests)}")
        for t in report.tests:
            metric = "" if t["metric"] is None else f" metric={t['metric']}"
            verdict = (
                "NOT RUN" if t["method"] == "not run" else ("PASS" if t["passed"] else "FAIL")
            )
            print(f"  {t['column']:<45} {t['test']:<45} {verdict}{metric} ({t['method']})")
    if report.hints and any(k != "unchanged" for k in report.hints):
        changed = {k: v for k, v in sorted(report.hints.items()) if k != "unchanged"}
        print(f"join hints     : {changed} (model hints whose test refuted or restored them)")
    if report.footprint is not None:
        fp = report.footprint
        print(
            f"footprint      : statistics {fp['statistics_before']} -> "
            f"{fp['statistics_after']}, new: {len(fp['new'])}"
        )
        for item in fp["new"][:50]:
            print(
                f"  + {item.get('table')}: {item.get('name')} "
                f"(column {item.get('leading_column')}, auto={item.get('auto_created')})"
            )
        print(f"                 {fp['note']}")
    if report.notes:
        print()
        print("notes:")
        for note in report.notes[:50]:
            print(f"  - {note}")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.unresolved_env:
        print(f"WARN unresolved env placeholders: {', '.join(settings.unresolved_env)}")
    options = ProfileOptions(
        target=args.target,
        schemas=args.schema or [],
        tables=args.table or [],
        with_neighbours=args.with_neighbours,
        limit=args.limit,
        dry_run=args.dry_run,
        ignore_window=args.ignore_window,
        force=args.force,
        joins=not args.no_joins,
    )
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            runner = run_stage3 if args.stage == 3 else run_profiling
            report = await runner(settings, session, options)
    finally:
        await dispose_engine()
    if args.json:
        print(json.dumps(report, default=lambda o: o.__dict__, indent=2))
    else:
        _print_report(report, detail=args.report)
    return 2 if report.count("failed") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect evidence from the target database.")
    parser.add_argument("--target", help="data source / target name from config")
    parser.add_argument("--schema", action="append", help="limit to a schema (repeatable)")
    parser.add_argument("--table", action="append", help="schema.table or table (repeatable)")
    parser.add_argument(
        "--with-neighbours",
        action="store_true",
        help="add tables linked to the selection by a declared foreign key",
    )
    parser.add_argument("--limit", type=int, help="at most N tables (after sorting by name)")
    parser.add_argument(
        "--stage",
        type=int,
        choices=(1, 3),
        default=1,
        help="1 = light profile + join candidates (default); 3 = tests of the stored claims",
    )
    parser.add_argument("--dry-run", action="store_true", help="the plan only; no target reads")
    parser.add_argument(
        "--ignore-window", action="store_true", help="run outside the configured profiling window"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Stage 1: re-profile fresh tables; Stage 3: re-run claim tests",
    )
    parser.add_argument("--no-joins", action="store_true", help="Stage 1 without join discovery")
    parser.add_argument(
        "--report",
        action="store_true",
        help="every join candidate tested, and every note of every table",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ProfilingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
