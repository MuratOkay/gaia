"""Evaluation runs and their items (Phase 6A, D80; migration 0018).

A **run** is one pass of an evaluation over a data source — today a golden run — with what it
ran on: the build, the git commit, the model, the question set (identified by its content, so
runs of the same questions compare and runs of different ones never do), the person it answered
as (``--as``), and whether its answers were asked **fresh**, **replayed** from recordings, a
**mix** (an interrupted run resumed) or **imported** from an older run's files. Only fresh,
mixed and imported runs are measurements; a replay reproduces one.

Since D81 a run also keeps a fingerprint of the **meanings** it answered with (migration
``0019``): an SME's decision changes what the model reads as surely as a new build does.

An **item** is one question of a run: what was expected, what Gaia did first and last, whether
the final answer was right, the best guess, repairs, calls, tokens and time. ``make quality``
reads these: intervals, questions that flip between runs, and comparisons question by question.
Since D84 an item also keeps what its final answer was made of (``detail``, migration ``0020``):
the context's objects and the expected objects it lacked, the interpretation and assumptions,
the clarification it was given, the confidence it showed.

A **label** (6B part 2, D85) is a person's verdict that does not live elsewhere: the owner's 3C
audit of Gaia's column meanings, imported from its files, with the judge's verdict beside it
(the judge's agreement with people, κ). SME decisions are labels too, read where they are
(``gaia.evaluation.human_labels``). A **calibrator** (6C, D86) is a fitted map from a raw score
to P(right) for one model and one scoring version, with the labels it was fitted on, how it did
on held-out labels against the identity, and whether it is the one in use.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gaia.db.models.base import Base
from gaia.db.models.catalog import new_uuid

S = "evaluation"
RUN_KINDS = ("golden",)
RUN_MODES = ("fresh", "replay", "mixed", "imported")
VERDICTS = ("right", "partly", "wrong")
CALIBRATION_KINDS = ("meaning", "answer")
CALIBRATION_METHODS = ("identity", "platt", "isotonic")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class EvaluationRun(Base):
    __tablename__ = "run"
    __table_args__ = (
        sa.CheckConstraint(f"kind IN ({', '.join(repr(k) for k in RUN_KINDS)})", name="kind"),
        sa.CheckConstraint(f"mode IN ({', '.join(repr(m) for m in RUN_MODES)})", name="mode"),
        sa.Index("ix_run_source_set", "data_source", "question_set_key", "started_at"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_source: Mapped[str] = mapped_column(sa.Text, nullable=False)  # a name: runs outlive it
    question_set: Mapped[str | None] = mapped_column(sa.Text)  # the file, as given
    question_set_key: Mapped[str] = mapped_column(sa.Text, nullable=False)  # of its content
    out_dir: Mapped[str | None] = mapped_column(sa.Text)
    build: Mapped[str | None] = mapped_column(sa.Text)
    git_commit: Mapped[str | None] = mapped_column(sa.Text)
    model: Mapped[str | None] = mapped_column(sa.Text)
    as_login: Mapped[str | None] = mapped_column(sa.Text)
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False)
    live_calls: Mapped[int | None] = mapped_column(sa.Integer)
    replayed_calls: Mapped[int | None] = mapped_column(sa.Integer)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # D81: the meanings the run answered with — a fingerprint of the data source's current
    # semantic versions, SME decisions, glossary and confirmed joins, and readable counts; a
    # comparison of runs on different meanings measures them too, not only the build
    meanings_key: Mapped[str | None] = mapped_column(sa.Text)
    meanings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class EvaluationItem(Base):
    __tablename__ = "item"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "item_key"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.run.id", ondelete="CASCADE"), nullable=False)
    item_key: Mapped[str] = mapped_column(sa.Text, nullable=False)  # the question's id
    question: Mapped[str | None] = mapped_column(sa.Text)
    expect: Mapped[str | None] = mapped_column(sa.Text)  # answer | clarify
    first_status: Mapped[str | None] = mapped_column(sa.Text)
    final_status: Mapped[str | None] = mapped_column(sa.Text)
    correct: Mapped[bool | None] = mapped_column(sa.Boolean)  # None: no reference
    correct_first: Mapped[bool | None] = mapped_column(sa.Boolean)
    executable: Mapped[bool | None] = mapped_column(sa.Boolean)
    over_clarified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    answered_directly: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    best_guess: Mapped[str | None] = mapped_column(sa.Text)
    ambiguity: Mapped[str | None] = mapped_column(sa.Text)
    repairs: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    seconds: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    sql: Mapped[str | None] = mapped_column(sa.Text)  # the final answer's (D81, 0019)
    # what the final answer was made of (D84, 0020): objects, missing, interpretation,
    # assumptions, clarification, confidence
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Label(Base):
    """A person's verdict on one of Gaia's column meanings, kept here because it does not live
    elsewhere (6B part 2): today the owner's 3C audit. ``origin`` names where it came from, so
    an import is idempotent."""

    __tablename__ = "label"
    __table_args__ = (
        sa.UniqueConstraint("source", "origin"),
        sa.CheckConstraint(_in("verdict", VERDICTS), name="verdict"),
        sa.CheckConstraint(f"judge_verdict IS NULL OR {_in('judge_verdict', VERDICTS)}",
                           name="judge_verdict"),
        sa.Index("ix_label_source_ds", "data_source", "source"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)  # 3c-audit
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)  # run dir | condition | column
    data_source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)  # schema.table.column
    verdict: Mapped[str] = mapped_column(sa.Text, nullable=False)
    judge_verdict: Mapped[str | None] = mapped_column(sa.Text)
    condition: Mapped[str | None] = mapped_column(sa.Text)  # 3C: N-P, B-P …
    production: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    model: Mapped[str | None] = mapped_column(sa.Text)
    scoring: Mapped[str | None] = mapped_column(sa.Text)  # the score's weights version
    raw_score: Mapped[float | None] = mapped_column(sa.Float)
    reviewer: Mapped[str | None] = mapped_column(sa.Text)
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=sa.func.now())


class Calibrator(Base):
    """A map from a raw score to P(right), fitted on labels (6C). One is in use per kind, model
    and scoring version at most; the identity is what is used when none is."""

    __tablename__ = "calibrator"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", CALIBRATION_KINDS), name="kind"),
        sa.CheckConstraint(_in("method", CALIBRATION_METHODS), name="method"),
        sa.Index("uq_calibrator_in_use", "kind", "model", "scoring", unique=True,
                 postgresql_where=sa.text("in_use")),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    version: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # meaning | answer
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scoring: Mapped[str] = mapped_column(sa.Text, nullable=False)
    method: Mapped[str] = mapped_column(sa.Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    labels: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    labels_wrong: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    labels_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    in_use: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    fitted_by: Mapped[str | None] = mapped_column(sa.Text)
    fitted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                server_default=sa.func.now())
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
