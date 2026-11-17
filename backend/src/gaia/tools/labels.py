"""Labels from people (Phase 6B, D83): SME decisions as verdicts on Gaia's meanings.

    make labels TARGET=demo_pg                   # the summary and every label
    make labels TARGET=demo_pg ARGS="--csv evaluations/labels_demo_pg.csv"

Each label is a decision on one machine meaning: right, partly or wrong (``gaia.evaluation.
human_labels`` says how), with the model, prompt and scoring version of the meaning judged
and its score — what calibration (6C) is fitted on.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import ROOT, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.evaluation.human_labels import decision_labels, label_summary


def summary_lines(summary: dict) -> list[str]:
    c = summary["counts"]
    lines = [f"{summary['n']} label(s) from SME decisions: {c['right']} right, "
             f"{c['partly']} partly, {c['wrong']} wrong"]
    if summary["auroc"] is not None:
        lines.append(f"Gaia's score ranks them with AUROC {summary['auroc']:.2f} "
                     "(0.5: no better than chance; 1: perfectly)")
    for m, x in summary["models"].items():
        au = f", AUROC {x['auroc']:.2f}" if x["auroc"] is not None else ""
        lines.append(f"  {m}: {x['n']} — {x['right']} right, {x['partly']} partly, "
                     f"{x['wrong']} wrong{au}")
    for b in summary["bands"]:
        if b["n"]:
            lines.append(f"  score {b['from']:.2f}–{b['to']:.2f}: {b['n']} label(s), "
                         f"{b['right']:.0%} right (partly counts half)")
    a = summary["anchoring"]
    if a["blind"]:
        lines.append(f"  accepted with Gaia's certainty on screen: {a['accepted_shown']} of "
                     f"{a['shown']}; without it (blind, P6-5): {a['accepted_blind']} of "
                     f"{a['blind']}")
    return lines


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            labels = await decision_labels(s, label)
    finally:
        await dispose_engine()
    if not labels:
        print(f"no SME decision on {label} yet: the review screen (Review) makes them")
        return 0
    for line in summary_lines(label_summary(labels)):
        print(line)
    print()
    for x in labels:
        print(f"  {x.verdict:<7} {x.column:<45} {x.action:<8} score {x.raw_score:.2f}  "
              f"{x.model}  by {x.reviewer}" + (f"  changed: {', '.join(x.changed)}"
                                                 if x.changed else "")
              + ("  (stale)" if x.stale else "") + ("  (blind)" if x.blind else ""))
    if args.csv:
        path = Path(args.csv) if Path(args.csv).is_absolute() else ROOT / args.csv
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
    p = argparse.ArgumentParser(description="Labels from SME decisions (6B).")
    p.add_argument("--target")
    p.add_argument("--csv", help="write every label to this CSV")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
