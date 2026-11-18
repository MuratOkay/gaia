"""Show the SME review queue (Phase 3B; decisions are taken in the web UI, Review — 5B).

    python -m gaia.tools.review --target demo_pg
    make review TARGET=demo_pg ARGS="--limit 100"
    make review TARGET=demo_pg ARGS="--sample"      # 7A: draw the week's sample, if not yet

Lists open items, highest priority first, with why each column needs an SME and what Gaia
currently believes it means (the operator's view: scores included). ``--sample`` draws the
week's calibration sample where it is not drawn yet (P7-2 — the review screens do the same when
an SME opens them) and says what it holds; nothing else here writes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.semantics.sampling import draw, sample_status


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
            if args.sample:
                result = await draw(session, source.id, cfg=settings.calibration)
                await session.commit()
                status = await sample_status(session, source.id, cfg=settings.calibration)
                short = "; ".join(f"{b}: {n} short" for b, n in
                                  (status["this_week"].get("short") or {}).items())
                print(f"{label}: the calibration sample of the week from "
                      f"{result['period']:%Y-%m-%d} — "
                      f"{'drawn now' if result['new'] else 'drawn before'}: "
                      f"{status['this_week']['drawn']} item(s), {status['this_week']['open']} "
                      f"waiting" + (f" ({short}: too few meanings in the band)" if short
                                    else ""))
                print("  this week by band of Gaia's score: " + ", ".join(
                    f"{b} {n}" for b, n in status["this_week"].get("bands", {}).items()))
                print(f"  in all: {status['drawn']} drawn over {status['weeks']} week(s), "
                      f"{status['decided']} decided, {status['open']} waiting, "
                      f"{status['lapsed']} lapsed\n")
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
    parser.add_argument("--sample", action="store_true",
                        help="draw the week's calibration sample if not drawn yet (7A)")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
