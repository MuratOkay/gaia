"""People's "Was this right?" across every data source (Phase 6D; D94).

    make feedback                      # the last 50, newest first
    make feedback ARGS="--last 10"

Each line: when, the data source, who said it, the verdict, the status (``none`` for a yes,
``open`` while an SME has not decided, ``decided``), the note if any, and who reviews that data
source. A doubt on a data source **nobody reviews** waits for no one — the line says so. This is
the operator's view: the SME sees only their own data sources (*Review → Answers people
doubted*), and the person only their own answers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.db.engine import dispose_engine, get_engine
from gaia.evaluation.feedback import all_feedback


async def _run(args: argparse.Namespace) -> int:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            rows = await all_feedback(s, limit=args.last)
    finally:
        await dispose_engine()
    if not rows:
        print("no \"Was this right?\" given yet")
        return 0
    for r in reversed(rows):
        status = r["status"] if r["status"] != "decided" else (
            f"decided by {r['decided_by']}: {r['decision']}")
        who = ", ".join(r["reviewers"]) if r["reviewers"] else "NOBODY reviews this data source"
        note = f"“{r['note'][:60]}”" if r["note"] else "no note"
        print(f"  {r['updated_at']:%Y-%m-%d %H:%M}  {r['data_source']:<12} {r['given_by']:<10} "
              f"{r['verdict']:<7} {status:<28} {note}")
        print(f"      {(r['question'] or '')[:80]}  — reviewed by: {who}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="People's \"Was this right?\" (6D).")
    p.add_argument("--last", type=int, default=50)
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
