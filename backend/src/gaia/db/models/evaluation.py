"""Evaluation runs and their items (Phase 6A, D80; migration 0018).

A **run** is one pass of an evaluation over a data source — today a golden run — with what it
ran on: the build, the git commit, the model, the question set (identified by its content, so
runs of the same questions compare and runs of different ones never do), the person it answered
as (``--as``), and whether its answers were asked **fresh**, **replayed** from recordings, a
**mix** (an interrupted run resumed) or **imported** from an older run's files. Only fresh,
mixed and imported runs are measurements; a replay reproduces one.

An **item** is one question of a run: what was expected, what Gaia did first and last, whether
the final answer was right, the best guess, repairs, calls, tokens and time. ``make quality``
reads these: intervals, questions that flip between runs, and comparisons question by question.
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
