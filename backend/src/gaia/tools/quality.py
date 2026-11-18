"""The quality report over golden runs (Phase 6A, D80).

    make quality TARGET=demo_pg                          # the last 3 measured runs
    make quality TARGET=demo_pg ARGS="--last 5"
    make quality TARGET=demo_pg ARGS="--runs 1a2b,3c4d"  # these runs (id prefixes)
    make quality TARGET=demo_pg ARGS="--baseline 1a2b,3c4d,5e6f --candidate 7a8b,9c0d,1e2f"
    make quality TARGET=demo_pg ARGS="--compare-builds"  # newest build's runs vs the one before
    make quality TARGET=demo_pg ARGS="--list"            # every run recorded, replays marked
    make quality TARGET=demo_pg ARGS="--as acme/finn"    # 7B: the runs made as that person
    make quality ARGS="--import 'evaluations/golden_demo_pg*'"   # older runs' files into the store

Every ``make golden`` is recorded (since D80); replays are listed and never counted. The
report is printed and written to ``evaluations/quality_<data source>_<time>.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth.service import cli_actor
from gaia.core.config import ROOT, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.evaluation.quality import quality_report, select_runs
from gaia.evaluation.sections import extra_sections
from gaia.evaluation.store import import_golden_dir, load_runs


def _ids(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            if args.import_glob:
                done = 0
                for pattern in args.import_glob:
                    base = pattern if Path(pattern).is_absolute() else str(ROOT / pattern)
                    for path in sorted(glob.glob(base)):
                        run = await import_golden_dir(s, Path(path), created_by=cli_actor())
                        if run is not None:
                            done += 1
                            print(f"imported {path} as run {str(run.id)[:8]} "
                                  f"({run.data_source}, {run.build or 'build ?'})")
                await s.commit()
                print(f"{done} run(s) imported")
                return 0
            label = args.target or settings.app.default_target
            unknown = await SemanticsStore(s).unknown_name(label, settings.targets)
            if unknown:
                print(unknown, file=sys.stderr)
                return 2
            runs = await load_runs(s, label)
            # 6B–6D: the meanings against people, the calibrators, feedback on answers
            extra, _extra_doc = await extra_sections(s, label, settings, factory)
            if not runs:
                print(f"no golden run of {label} recorded yet: `make golden` (or --import)")
                return 1
            if args.list:
                for r in runs:
                    scored = [i for i in r.items.values() if i.correct is not None]
                    print(f"  {r.id[:8]}  {r.started_at:%Y-%m-%d %H:%M}  {r.mode:<8} "
                          f"{(r.build or '—'):<40} {r.model or '—':<16} "
                          f"{sum(1 for i in scored if i.correct)}/{len(scored)}"
                          + (f"  as {r.as_login}" if r.as_login else ""))
                return 0
        sel = select_runs(runs, ids=_ids(args.runs), set_key=args.set, last=args.last,
                          baseline_ids=_ids(args.baseline), candidate_ids=_ids(args.candidate),
                          compare_builds=args.compare_builds, as_login=args.as_login)
        if (args.baseline or args.candidate or args.compare_builds) and not sel.baseline:
            print("note: no comparison — name measured runs of one question set with "
                  "--baseline and --candidate, or record runs of two builds", file=sys.stderr)
        text, _doc = quality_report(sel.shown, data_source=label, question_set_key=sel.set_key,
                                    excluded=sel.excluded, baseline=sel.baseline,
                                    candidate=sel.candidate, regressions=sel.regressions,
                                    history=sel.history, trials=sel.trials)
        text += extra
        print(text)
        out = ROOT / "evaluations" / f"quality_{label}_{datetime.now(UTC):%Y%m%d_%H%M}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"written to {out}")
        return 0
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The quality report over golden runs (6A).")
    p.add_argument("--target")
    p.add_argument("--last", type=int, default=3, help="the last N measured runs (default 3)")
    p.add_argument("--runs", help="run ids (prefixes), comma separated")
    p.add_argument("--set", help="a question set's key (default: the newest run's)")
    p.add_argument("--baseline", help="run ids of the baseline")
    p.add_argument("--candidate", help="run ids of the candidate")
    p.add_argument("--compare-builds", action="store_true",
                   help="the newest build's runs against the build before")
    p.add_argument("--list", action="store_true")
    p.add_argument("--as", dest="as_login", metavar="LOGIN",
                   help="the runs made as this person (make golden --as); without it, the "
                        "operator's runs only")
    p.add_argument("--import", dest="import_glob", action="append",
                   help="older golden run directories (a glob), into the store")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
