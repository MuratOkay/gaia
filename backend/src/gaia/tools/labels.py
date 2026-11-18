"""Labels from people (Phase 6B, D83, D85): verdicts on Gaia's meanings.

    make labels TARGET=demo_pg                   # the summary and every label
    make labels TARGET=demo_pg ARGS="--csv evaluations/labels_demo_pg.csv"
    make labels ARGS="--import-3c evaluations/nge_small"      # the owner's filled 3C audit

Each label is a verdict on one machine meaning: right, partly or wrong (``gaia.evaluation.
human_labels`` says how), with the model, prompt and scoring version of the meaning judged
and its score — what calibration (6C) is fitted on. Two sources: SME decisions (read where they
are), and the owner's 3C audit (``<run>/audit.csv``, imported once into ``evaluation.label``,
with the judge's verdict beside each — the judge's agreement with people).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth.service import cli_actor
from gaia.core.config import ROOT, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.evaluation.human_labels import (
    all_labels,
    import_3c_audit,
    label_summary,
    read_3c_audit,
)
from gaia.evaluation.sections import summary_lines


def _path(text: str) -> Path:
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


async def _import(args: argparse.Namespace) -> int:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    out = _path(args.import_3c)
    try:
        async with factory() as s:
            try:
                files = read_3c_audit(out, _path(args.audit) if args.audit else None)
                r = await import_3c_audit(s, files, data_source=args.target,
                                          created_by=cli_actor(), reviewer=args.reviewer)
            except (OSError, ValueError) as exc:
                print(f"cannot import: {exc}", file=sys.stderr)
                return 1
            await s.commit()
    finally:
        await dispose_engine()
    print(f"{r['data_source']}: {r['imported']} audit verdict(s) imported, {r['known']} known "
          f"already, {r['unfilled']} row(s) without the owner's verdict")
    for m in r["missing"]:
        print(f"  not in the run's results (left out): {m}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.import_3c:
        return await _import(args)
    settings = get_settings()
    label = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            labels = await all_labels(s, label)
            unknown = None if labels else await SemanticsStore(s).unknown_name(
                label, settings.targets)
    finally:
        await dispose_engine()
    if unknown:
        print(unknown, file=sys.stderr)
        return 2
    if not labels:
        print(f"no label on {label} yet: the review screen (Review) makes them, and "
              "--import-3c brings the owner's 3C audit in")
        return 0
    for line in summary_lines(label_summary(labels)):
        print(line)
    print()
    for x in labels:
        by = f"by {x.reviewer}" if x.reviewer else ("3C audit" if x.source == "3c-audit"
                                                    else "")
        print(f"  {x.verdict:<7} {x.column:<45} {x.action:<8} score {x.raw_score:.2f}  "
              f"{x.model}  {by}" + (f"  changed: {', '.join(x.changed)}"
                                    if x.changed else "")
              + (f"  ({x.prompt_version}, judge: {x.judge or '—'})" if x.source == "3c-audit"
                 else "")
              + ("  (stale)" if x.stale else "")
              + ("  (blind)" if x.blind and x.source == "sme" else ""))
    if args.csv:
        path = _path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [x.as_dict() for x in labels]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow({**r, "changed": "|".join(r["changed"])})
        print(f"\nwritten to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Labels from people (6B).")
    p.add_argument("--target")
    p.add_argument("--csv", help="write every label to this CSV")
    p.add_argument("--import-3c", metavar="DIR",
                   help="a 3C run directory whose audit.csv the owner filled")
    p.add_argument("--audit", help="with --import-3c: the audit file (default DIR/audit.csv)")
    p.add_argument("--reviewer", help="with --import-3c: who audited (kept with each label)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
