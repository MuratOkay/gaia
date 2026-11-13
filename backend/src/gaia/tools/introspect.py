"""Run a catalog introspection pass.

Usage:
    python -m gaia.tools.introspect            # read, diff, persist
    python -m gaia.tools.introspect --dry-run  # read and diff only
    python -m gaia.tools.introspect --json     # also dump the snapshot document

Exits non-zero on failure. Designed to be idempotent: a second run against an
unchanged schema writes nothing but a new snapshot record and reports every
object as unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.introspect.service import run_introspection


async def _run(dry_run: bool, out_json: Path | None, target: str | None) -> int:
    settings = get_settings()
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            run = await run_introspection(
                settings, session, target_name=target, dry_run=dry_run
            )
            if not dry_run:
                await session.commit()
    finally:
        await dispose_engine()

    snapshot = run.snapshot
    summary = run.diff.summary()

    print(f"target        : {target or settings.app.default_target}")
    print(f"database      : {snapshot.database}")
    print(f"server        : {snapshot.server_version}")
    print(f"capture       : {'first' if run.is_first_capture else 'incremental'}")
    print(f"objects       : {len(snapshot.tables)} "
          f"({sum(1 for t in snapshot.tables if t.kind == 'table')} tables, "
          f"{sum(1 for t in snapshot.tables if t.kind == 'view')} views)")
    print(f"columns       : {snapshot.column_count}")
    print(f"structure_hash: {snapshot.structure_hash[:16]}")
    print(f"data_hash     : {snapshot.data_hash[:16]}")
    print()
    print(f"diff          : added={summary['added']} removed={summary['removed']} "
          f"structure_changed={summary['structure_changed']} "
          f"data_changed={summary['data_changed']} "
          f"unchanged={summary['unchanged']}")
    print(f"needs reasoning: {summary['needs_reasoning']}")

    if not dry_run:
        result = run.result
        print()
        print(f"snapshot_id   : {result.snapshot_id}")
        print(f"tables written: {result.tables_written} "
              f"(unchanged {result.tables_unchanged}, "
              f"tombstoned {result.tables_tombstoned})")
        print(f"columns written: {result.columns_written}")
        print(f"relations     : {result.relations_written}")

    changed = [c for c in run.diff.changes]
    if changed:
        print()
        print("changes:")
        for change in changed[:40]:
            print(f"  - {change.summary()}")
        if len(changed) > 40:
            print(f"  ... and {len(changed) - 40} more")

    if out_json is not None:
        out_json.write_text(
            json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nsnapshot document written to {out_json}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Introspect the target catalog.")
    parser.add_argument("--dry-run", action="store_true", help="do not persist")
    parser.add_argument("--json", type=Path, help="write the snapshot document here")
    parser.add_argument("--target", help="target name from config")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args.dry_run, args.json, args.target))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
