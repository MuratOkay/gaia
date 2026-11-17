"""Validate and run one SQL statement through the guard (Phase 4A).

    python -m gaia.tools.query --target demo_pg --sql "SELECT count(*) FROM sales.orders"
    python -m gaia.tools.query --target demo_pg --file q.sql --dry-run
    make query TARGET=demo_pg ARGS="--sql 'SELECT …'"
    make query-check TARGET=demo_pg ARGS="--sql 'SELECT …'"      # guard only, no execution

The guard (``gaia.query.validator``) checks the statement against the stored
catalog snapshot and join paths; only what it renders is executed, read-only,
with the target's statement timeout and the row cap. Every run — accepted or
rejected — is recorded in ``chat.query_run`` (``--no-record`` to skip), pinned to
the snapshot and the semantic versions of what it read. ``--dry-run`` only
validates and records nothing.

Exit codes: 0 ok, 2 rejected by the guard, 3 the execution failed or timed out.
This is the tool the SQL generator (Phase 4C) will use; today it lets a person
check what the guard allows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.query_store import QueryStore
from gaia.query.catalog import load_guard_catalog
from gaia.query.executor import ExecutionResult, execute
from gaia.query.guard import build_guard, row_limit
from gaia.query.validator import GuardResult

ROOT = Path(__file__).resolve().parents[4]


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _print_guard(g: GuardResult) -> None:
    print(f"guard          : {'ACCEPTED' if g.ok else 'REJECTED'} ({g.dialect})")
    for v in g.errors:
        print(f"  error        : {v}")
    for v in g.warnings:
        print(f"  warning      : {v}")
    if g.tables:
        print(f"objects        : {', '.join(g.tables)}")
    for j in g.joins:
        mark = "ok " if j.verified else "NO "
        print(f"join           : {mark}{j.kind:<11} {j.left or '?'} = {j.right or '?'}")
    if g.ok:
        print(f"row cap        : {g.row_limit}" + (" (added)" if g.limit_injected else ""))
        print("sql            :")
        for line in (g.sql or "").splitlines():
            print(f"  {line}")


def _print_result(r: ExecutionResult, shown: int) -> None:
    print(f"execution      : {r.status} in {r.elapsed_ms} ms"
          + (f"; {len(r.rows)} row(s)" if r.ok else "")
          + ("; row cap reached (there may be more)" if r.row_cap_reached else ""))
    if not r.ok:
        print(f"  error        : {r.error}")
        return
    rows = [[_cell(v) for v in row] for row in r.rows[:shown]]
    widths = [max([len(c)] + [len(row[i]) for row in rows]) for i, c in enumerate(r.columns)]
    print("  " + " | ".join(c.ljust(w) for c, w in zip(r.columns, widths, strict=True)))
    print("  " + "-+-".join("-" * w for w in widths))
    for row in rows:
        print("  " + " | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    if len(r.rows) > shown:
        print(f"  … {len(r.rows) - shown} more row(s) (--rows N)")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    sql = args.sql
    if not sql or not sql.strip():
        print("give --sql or --file", file=sys.stderr)
        return 1
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                catalog, ds_id = await load_guard_catalog(session, label)
            except LookupError as exc:  # never introspected: say what to do (D76)
                print(f"FAILED: {exc}", file=sys.stderr)
                return 2
            guard = build_guard(settings, label, catalog).check(sql)
            execution = None
            if guard.ok and not args.dry_run:
                execution = await asyncio.to_thread(
                    execute, settings.target(label), guard, row_limit=row_limit(settings, label))
            run_id = None
            if not args.dry_run and not args.no_record:
                run = await QueryStore(session).record(
                    data_source_id=ds_id, snapshot_id=catalog.snapshot_id, guard=guard,
                    execution=execution, origin="cli", question=args.question)
                await session.commit()
                run_id = run.id
    finally:
        await dispose_engine()

    if args.json:
        doc = {"guard": guard.as_dict(), "run_id": str(run_id) if run_id else None}
        if execution is not None:
            doc["execution"] = {"status": execution.status, "columns": execution.columns,
                                "rows": [[_cell(v) for v in r] for r in execution.rows],
                                "row_cap_reached": execution.row_cap_reached,
                                "elapsed_ms": execution.elapsed_ms, "error": execution.error}
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        print(f"data source    : {label} (snapshot {catalog.snapshot_id})")
        _print_guard(guard)
        if execution is not None:
            _print_result(execution, args.rows)
        if run_id:
            print(f"recorded       : chat.query_run {run_id}")
    if not guard.ok:
        return 2
    return 0 if execution is None or execution.ok else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and run one SQL statement.")
    parser.add_argument("--target", help="data source name (default: app.default_target)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sql", help="the statement")
    source.add_argument("--file", help="a file holding the statement")
    parser.add_argument("--dry-run", action="store_true", help="validate only; record nothing")
    parser.add_argument("--no-record", action="store_true", help="do not write chat.query_run")
    parser.add_argument("--question", help="the business question, stored with the run")
    parser.add_argument("--rows", type=int, default=20, help="rows to print (default 20)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.file:
        path = Path(args.file)
        if not path.is_absolute() and not path.exists():
            path = ROOT / path  # relative to the repository root (where `make` runs)
        args.sql = path.read_text(encoding="utf-8")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
