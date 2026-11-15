"""Show the SME review queue (Phase 3B; the review UI is Phase 5).

    python -m gaia.tools.review --target demo_pg
    make review TARGET=demo_pg ARGS="--limit 100"

Read-only. Lists open items, highest priority first, with why each column needs
an SME and what Gaia currently believes it means.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.semantics_store import SemanticsStore


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            store = SemanticsStore(session)
            source = await store.data_source(label)
            if source is None:
                print(f"no data source {label!r}; run make introspect", file=sys.stderr)
                return 1
            items = await store.open_review_items(source.id, limit=args.limit)
    finally:
        await dispose_engine()
    if not items:
        print(f"{label}: the review queue is empty")
        return 0
    print(f"{label}: {len(items)} open review item(s), highest priority first\n")
    print(f"  {'priority':>8}  {'score':>5}  {'coverage':<13} {'column':<40} belief / reasons")
    for item, table, column, sem in items:
        name = f"{table.schema_name}.{table.object_name}.{column.column_name if column else '*'}"
        belief = f"{sem.business_name} ({sem.role})" if sem is not None else "?"
        print(f"  {item.priority:>8.3f}  {item.score:>5.3f}  {item.coverage:<13} {name:<40} "
              f"{belief}  [{', '.join(item.reasons)}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the SME review queue.")
    parser.add_argument("--target", help="data source name (default: app.default_target)")
    parser.add_argument("--limit", type=int, default=50)
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
