"""Calibrators in the metadata store, and the labels they are fitted on (6C D86, 6D D87).

* **Meanings** (kind ``meaning``): people's labels on column meanings (6B) — SME decisions and
  the owner's 3C audit of production-mode answers — grouped by the model that wrote the meaning
  and the scoring version of its score, pooled across data sources (a model's scores mean the
  same on every data source; a new data source starts from the others' calibration).
* **Answers** (kind ``answer``): the confidence score an answer showed, against whether it was
  right — measured golden runs (since D84 each item keeps the score it showed) and SMEs'
  decisions on people's "Was this right?" (6D) — grouped by the model that answered.

At most one calibrator is **in use** per kind, model and scoring version; none means the
identity. Putting one in use is an administrator's step (``make calibrate ARGS="--apply"``);
the meanings' stored scores follow with ``make rescore`` (no model call), answers from the next
question on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.evaluation import Calibrator, EvaluationItem, EvaluationRun
from gaia.evaluation.calibration import Fit, Point, Trial
from gaia.evaluation.human_labels import VALUE, HumanLabel

__all__ = ["ANSWER_SCORING", "answer_points", "in_use", "meaning_points", "put_in_use",
           "record_trial"]

ANSWER_SCORING = "confidence-v1"  # the heuristic of gaia.query.ask.confidence


def meaning_points(labels: Sequence[HumanLabel]) -> dict[tuple[str, str], list[Point]]:
    """Production labels by (model, scoring version)."""
    out: dict[tuple[str, str], list[Point]] = defaultdict(list)
    for x in labels:
        if x.production and x.model and x.scoring:
            out[(x.model, x.scoring)].append(Point(x.raw_score, VALUE[x.verdict],
                                                   f"{x.data_source}:{x.column}"))
    return dict(out)


async def answer_points(session: AsyncSession) -> dict[tuple[str, str], list[Point]]:
    """Answers scored against a reference or decided by an SME, by (model, scoring)."""
    out: dict[tuple[str, str], list[Point]] = defaultdict(list)
    rows = (await session.execute(
        select(EvaluationItem, EvaluationRun.model, EvaluationRun.data_source)
        .join(EvaluationRun, EvaluationRun.id == EvaluationItem.run_id)
        .where(EvaluationRun.mode != "replay", EvaluationItem.correct.is_not(None),
               EvaluationItem.final_status == "answered"))).all()
    for item, model, ds in rows:
        conf = (item.detail or {}).get("confidence") or {}
        if conf.get("score") is None or not model:
            continue
        out[(model, ANSWER_SCORING)].append(Point(
            float(conf["score"]), 1.0 if item.correct else 0.0, f"{ds}:{item.item_key}"))
    from gaia.db.models.chat import Feedback, Message, QueryRun

    decided = (await session.execute(
        select(Feedback, Message.payload, QueryRun.model)
        .join(Message, Message.id == Feedback.message_id)
        .outerjoin(QueryRun, QueryRun.id == Message.query_run_id)
        .where(Feedback.status == "decided", Feedback.decision.is_not(None)))).all()
    for fb, payload, model in decided:
        conf = (payload or {}).get("confidence") or {}
        if conf.get("score") is None or not model:
            continue
        out[(model, ANSWER_SCORING)].append(Point(
            float(conf["score"]), VALUE[fb.decision], f"feedback:{fb.message_id}"))
    return dict(out)


async def record_trial(session: AsyncSession, *, kind: str, model: str, scoring: str,
                       trial: Trial, fitted_by: str | None) -> Calibrator | None:
    """Keep a fitted calibrator (not in use); the identity is never stored. One row per
    version: fitting the same labels again finds it."""
    if not trial.fit.calibrated:
        return None
    found = await session.scalar(select(Calibrator).where(
        Calibrator.version == trial.fit.version, Calibrator.kind == kind,
        Calibrator.model == model, Calibrator.scoring == scoring))
    if found is not None:
        return found
    row = Calibrator(version=trial.fit.version, kind=kind, model=model, scoring=scoring,
                     method=trial.method, params=trial.fit.params, metrics=trial.as_dict(),
                     labels=trial.labels, labels_wrong=trial.wrong, labels_key=trial.key,
                     in_use=False, fitted_by=fitted_by)
    session.add(row)
    await session.flush()
    return row


async def put_in_use(session: AsyncSession, row: Calibrator | None, *, kind: str, model: str,
                     scoring: str) -> None:
    """``row`` in use for its kind, model and scoring version; ``None``: back to the identity."""
    await session.execute(update(Calibrator).where(
        Calibrator.kind == kind, Calibrator.model == model, Calibrator.scoring == scoring,
        Calibrator.in_use.is_(True)).values(in_use=False))
    await session.flush()
    if row is not None:
        row.in_use = True
        row.applied_at = datetime.now(UTC)
        await session.flush()


async def in_use(session: AsyncSession, kind: str) -> dict[tuple[str, str], Fit]:
    """The calibrators in use for a kind, by (model, scoring version)."""
    rows = (await session.scalars(select(Calibrator).where(
        Calibrator.kind == kind, Calibrator.in_use.is_(True)))).all()
    return {(r.model, r.scoring): Fit(r.method, dict(r.params or {}), r.version) for r in rows}


def describe(rows: Sequence[Calibrator]) -> list[dict[str, Any]]:
    return [{"version": r.version, "kind": r.kind, "model": r.model, "scoring": r.scoring,
             "method": r.method, "labels": r.labels, "labels_wrong": r.labels_wrong,
             "in_use": r.in_use, "fitted_at": r.fitted_at, "applied_at": r.applied_at,
             "metrics": r.metrics} for r in rows]
