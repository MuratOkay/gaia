"""Calibrators: fit, judge on held-out labels, put in use (Phase 6C D86, 6D D87).

    make calibrate                                   # every model: fit and judge; changes nothing
    make calibrate ARGS="--kind meaning --model deepseek-flash --apply"
    make calibrate ARGS="--kind answer --model deepseek-flash --apply"
    make calibrate ARGS="--kind meaning --model deepseek-flash --reset"   # back to the identity
    make calibrate ARGS="--list"                     # every calibrator kept, and which is in use

A calibrator is fitted per model and scoring version on people's labels (meanings: SME
decisions and the owner's 3C audit, ``make labels``; answers: measured golden runs and SMEs'
decisions on "Was this right?"). The method follows P6-1's minimums (``calibration.*``); it is
judged on labels it was not fitted on, against the identity, and ``--apply`` refuses one that
does not beat the identity on both the calibration error and the Brier score (``--force`` to put
it in use anyway — then say why in the decisions log). Below the minimums there is nothing to
apply: the scores stay uncalibrated, and say so.

After ``--apply`` of a meaning calibrator the stored scores follow with ``make rescore
TARGET=…`` for every data source of that model (no model call; new versions, the review queue
re-ranked); an answer calibrator applies from the next question on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth.service import cli_actor
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.models.evaluation import Calibrator
from gaia.evaluation.calibration import choose_method, trial
from gaia.evaluation.calibrators import (
    answer_points,
    meaning_points,
    put_in_use,
    record_trial,
)
from gaia.evaluation.human_labels import all_labels
from gaia.evaluation.sections import trial_lines


async def _data_sources_of(session, model: str, earlier: list[str] | None = None) -> list[str]:
    from gaia.db.models.catalog import DataSource, TableRecord
    from gaia.db.models.semantics import ColumnSemantic

    rows = await session.scalars(
        select(DataSource.name).join(TableRecord, TableRecord.data_source_id == DataSource.id)
        .join(ColumnSemantic, ColumnSemantic.table_id == TableRecord.id)
        .where(ColumnSemantic.model.in_([model, *(earlier or [])]),
               ColumnSemantic.is_current.is_(True)).distinct())
    return sorted(rows.all())


def _reliability(t) -> list[str]:
    if t.held_out is None:
        return []
    out = ["    predicted (held out)   observed   labels"]
    for row in t.held_out["reliability"]:
        out.append(f"    {row['from']:.1f}–{row['to']:.1f}: {row['predicted']:>8.2f}   "
                   f"{row['observed']:>8.2f}   {row['n']:>6}")
    return out


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            if args.list:
                rows = (await s.scalars(select(Calibrator).order_by(
                    Calibrator.kind, Calibrator.model, Calibrator.fitted_at))).all()
                if not rows:
                    print("no calibrator fitted yet: every score is the identity (uncalibrated)")
                for r in rows:
                    m = r.metrics or {}
                    held = m.get("held_out") or {}
                    print(f"  {'IN USE ' if r.in_use else '       '}{r.kind:<8} {r.model:<20} "
                          f"{r.scoring:<14} {r.version:<22} {r.labels:>4} labels "
                          f"({r.labels_wrong} not right)  ECE {held.get('ece')}  Brier "
                          f"{held.get('brier')}  fitted {r.fitted_at:%Y-%m-%d %H:%M}")
                return 0
            groups = []
            aliases = settings.calibration.model_aliases  # P6-9: a provider's renames
            if args.kind in (None, "meaning"):
                for key, pts in meaning_points(await all_labels(s), aliases).items():
                    groups.append(("meaning", key, pts))
            if args.kind in (None, "answer"):
                for key, pts in (await answer_points(s, aliases)).items():
                    groups.append(("answer", key, pts))
            if args.model:
                groups = [g for g in groups if g[1][0] == args.model]
            if not groups:
                print("no labels for that yet" if args.model or args.kind else
                      "no labels yet: SME decisions (Review), `make labels ARGS=\"--import-3c "
                      "…\"`, golden runs (since D84) and decided feedback make them")
                return 1 if (args.apply or args.reset) else 0
            if (args.apply or args.reset) and (not args.kind or not args.model):
                print("--apply and --reset need --kind and --model", file=sys.stderr)
                return 2
            used = {(r.kind, r.model, r.scoring): r.version for r in (await s.scalars(
                select(Calibrator).where(Calibrator.in_use.is_(True)))).all()}
            code = 0
            for kind, (model, scoring), points in sorted(groups, key=lambda g: (g[0], g[1])):
                t = trial(points, settings.calibration, args.method)
                for line in trial_lines(kind, model, scoring, t,
                                        used.get((kind, model, scoring)),
                                        settings.calibration.earlier_names(model)):
                    print(line)
                for line in _reliability(t):
                    print(line)
                if args.reset:
                    await put_in_use(s, None, kind=kind, model=model, scoring=scoring)
                    print("  → back to the identity")
                    continue
                row = await record_trial(s, kind=kind, model=model, scoring=scoring, trial=t,
                                         fitted_by=cli_actor())
                if not args.apply:
                    continue
                if row is None:
                    print("  → nothing to apply: too few labels (the identity stays)")
                    code = 1
                    continue
                if choose_method(points, settings.calibration)[0] == "identity" and \
                        not args.force:
                    print("  → not applied: below P6-1's minimum of labels (--force to apply "
                          "anyway)")
                    code = 1
                    continue
                if not t.improves and not args.force:
                    print("  → not applied: it does not beat the identity on held-out labels "
                          "(--force to apply anyway)")
                    code = 1
                    continue
                await put_in_use(s, row, kind=kind, model=model, scoring=scoring)
                print(f"  → in use: {row.version}")
                if kind == "meaning":
                    for ds in await _data_sources_of(
                            s, model, settings.calibration.earlier_names(model)):
                        print(f"     now: make rescore TARGET={ds}   (no model call)")
                else:
                    print("     answers use it from the next question on")
                print()
            await s.commit()
            return code
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Calibrators: fit, judge, put in use (6C, 6D).")
    p.add_argument("--kind", choices=["meaning", "answer"])
    p.add_argument("--model")
    p.add_argument("--method", choices=["identity", "platt", "isotonic"],
                   help="instead of the one P6-1's minimums choose")
    p.add_argument("--apply", action="store_true", help="put the fitted calibrator in use")
    p.add_argument("--force", action="store_true",
                   help="with --apply: even if it does not beat the identity")
    p.add_argument("--reset", action="store_true", help="back to the identity")
    p.add_argument("--list", action="store_true")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
