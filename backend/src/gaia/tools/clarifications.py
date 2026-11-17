"""The clarifications Gaia asked and how users answered them (D61).

    make clarifications TARGET=demo_pg                 # users' clarifications (cli, api)
    make clarifications TARGET=demo_pg ARGS="--all"     # golden and test runs too

Per question asked back: the options, whether Gaia had a best guess, and the user's
choice. Then the measurement a later "answer without asking" needs, per kind of
ambiguity: how many were answered by choosing an option, and in how many of those
the user chose the option Gaia would have chosen. Free-text answers count as answered
but not as agreement either way (they were not one of Gaia's options).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.query_store import QueryStore
from gaia.db.repositories.semantics_store import SemanticsStore


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    origins = ("cli", "api", "golden", "test") if args.all else ("cli", "api")
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            ds = await SemanticsStore(session).data_source(label)
            if ds is None:
                print(f"no data source {label!r}", file=sys.stderr)
                return 1
            rows = await QueryStore(session).clarification_stats(ds.id, include=origins)
    finally:
        await dispose_engine()
    print(f"{label}: {len(rows)} clarification(s) ({', '.join(origins)})\n")
    for r in rows[-args.limit:]:
        guess = "-" if r.default_option is None else str(r.default_option + 1)
        chose = ("open" if r.status == "open" else
                 "own words" if r.chosen_option is None else str(r.chosen_option + 1))
        agree = {True: "agrees", False: "differs", None: ""}[r.took_default]
        print(f"  {r.created_at:%Y-%m-%d %H:%M} {r.origin:<6} {r.ambiguity or '?':<10} "
              f"guess {guess:<2} chose {chose:<9} {agree:<8} {r.question[:60]}")
    by_kind: dict[str, list] = defaultdict(list)
    for r in rows:
        by_kind[r.ambiguity or "?"].append(r)
    print("\nagreement (user chose Gaia's option / answered with an option):")
    for kind, items in sorted(by_kind.items()):
        picked = [r for r in items if r.took_default is not None]
        agreed = sum(1 for r in picked if r.took_default)
        share = f"{agreed / len(picked):.0%}" if picked else "—"
        print(f"  {kind:<10} {agreed}/{len(picked)} ({share}); {len(items)} asked, "
              f"{sum(1 for r in items if r.status == 'answered')} answered")
    # Position bias (5.0, T5): since the options are shuffled, the first shown option is
    # Gaia's only by chance; users who still pick it far more often than 1/n pick by place.
    shuffled = [r for r in rows if r.option_order is not None and r.chosen_option is not None]
    if shuffled:
        first = sum(1 for r in shuffled if r.chosen_option == 0)
        chance = sum(1 / len(r.options) for r in shuffled if r.options)
        print(f"\nposition: the first shown option was chosen {first} of {len(shuffled)} "
              f"time(s) (by chance ~{chance:.1f}); options are shown shuffled since D67 "
              "(2026-11-17)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clarifications and Gaia's agreement with users.")
    p.add_argument("--target")
    p.add_argument("--all", action="store_true", help="include golden and test runs")
    p.add_argument("--limit", type=int, default=50)
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
