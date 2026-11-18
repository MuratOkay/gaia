"""Operations (Phase 7C, P7-6, P7-7; D98): jobs, their schedules, their log, and alerts.

PostgreSQL is the source of truth for what runs (P7-6: not Redis, which is ephemeral by the
project's own rule). A **job** is one piece of work on a data source — a refresh, a drift
watch, the week's calibration sample — queued by a person or a **schedule**, claimed by the
worker with ``FOR UPDATE SKIP LOCKED``, with its progress, its log (``job_event``), what it
cost and how it ended. Per-data-source mutual exclusion stays D67's advisory locks, taken by
the work itself. An **alert** is what an administrator should look at: a failed job, a run
stopped by its budget, a drift the watch caught.
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

__all__ = ["ALERT_KINDS", "JOB_KINDS", "JOB_STATES", "MODEL_PURPOSES", "MODEL_STATUSES", "Alert",
           "Job", "JobEvent", "Model", "ModelAssignment", "Schedule"]

S = "ops"
JOB_KINDS = ("refresh", "drift_watch", "sample", "model_trial")
# queued → running → done | partial (stopped early: budget, window, cancel asked mid-step)
#                  | cancelled | failed | refused (could not start: busy, disabled, window)
JOB_STATES = ("queued", "running", "done", "partial", "cancelled", "failed", "refused")
ALERT_KINDS = ("job_failed", "job_partial", "drift", "model_changed", "worker_lost",
               "model_switched")
MODEL_PURPOSES = ("reasoning", "answering")  # meanings (with their critique) / SQL and narration
MODEL_STATUSES = ("unverified", "verified", "failed", "retired")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Schedule(Base):
    """When a kind of job is queued by itself for a data source: daily or weekly, at a local
    time of the host (as ``profiling.window`` is)."""

    __tablename__ = "schedule"
    __table_args__ = (
        sa.CheckConstraint(f"kind IN {_in(JOB_KINDS)}", name="kind"),
        sa.CheckConstraint("every IN ('daily', 'weekly')", name="every"),
        sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="weekday"),
        sa.UniqueConstraint("data_source", "kind", name="uq_schedule_source_kind"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    every: Mapped[str] = mapped_column(sa.Text, nullable=False)
    at_time: Mapped[str] = mapped_column(sa.Text, nullable=False)  # "HH:MM", host local time
    weekday: Mapped[int | None] = mapped_column(sa.SmallInteger)  # weekly: 0 Monday … 6
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    next_due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_queued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        sa.CheckConstraint(f"kind IN {_in(JOB_KINDS)}", name="kind"),
        sa.CheckConstraint(f"state IN {_in(JOB_STATES)}", name="state"),
        sa.Index("ix_job_state_created", "state", "created_at"),
        sa.Index("ix_job_source_created", "data_source", "created_at"),
        # at most one queued or running job of a kind per data source
        sa.Index("uq_job_active", "data_source", "kind", unique=True,
                 postgresql_where=sa.text("state IN ('queued', 'running')")),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="queued")
    requested_by: Mapped[str] = mapped_column(sa.Text, nullable=False)  # a login, "schedule"
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.schedule.id", ondelete="SET NULL"))
    worker: Mapped[str | None] = mapped_column(sa.Text)  # host:pid of the worker running it
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(sa.Text)
    tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancel_requested_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class JobEvent(Base):
    """One line of a job's log, in order."""

    __tablename__ = "job_event"
    __table_args__ = (
        sa.Index("ix_job_event_job", "job_id", "seq"),
        {"schema": S},
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.job.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    level: Mapped[str] = mapped_column(sa.Text, nullable=False, default="info")
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)


class Alert(Base):
    """Something an administrator should look at, until acknowledged."""

    __tablename__ = "alert"
    __table_args__ = (
        sa.CheckConstraint(f"kind IN {_in(ALERT_KINDS)}", name="kind"),
        sa.Index("ix_alert_open", "acknowledged_at", "created_at"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_source: Mapped[str | None] = mapped_column(sa.Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.job.id", ondelete="SET NULL"))
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    acknowledged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(sa.Text)


class Model(Base):
    """A model registered beside the configured one (Phase 7D, P7-4; D101): how to reach it,
    whether its probe passed, and since when it may no longer be chosen. The model the
    configuration names is not a row: it is always there, read-only (``gaia.models``)."""

    __tablename__ = "model"
    __table_args__ = (
        sa.CheckConstraint(f"status IN {_in(MODEL_STATUSES)}", name="status"),
        sa.CheckConstraint("locality IN ('local', 'remote')", name="locality"),
        sa.UniqueConstraint("name", name="uq_model_name"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_id: Mapped[str] = mapped_column(sa.Text, nullable=False)  # what the API is sent
    # the key: the name of an environment variable holding it, or encrypted with the master key
    key_env: Mapped[str | None] = mapped_column(sa.Text)
    secret: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    secret_key_id: Mapped[str | None] = mapped_column(sa.Text)
    locality: Mapped[str] = mapped_column(sa.Text, nullable=False, default="remote")  # D24
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unverified")
    probe: Mapped[list[Any] | None] = mapped_column(JSONB)
    probed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    retired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ModelAssignment(Base):
    """Which model serves a purpose, since when, and the one it replaced (a rollback returns
    to it while ``rollback_until``). One current row per purpose; none: the configured model."""

    __tablename__ = "model_assignment"
    __table_args__ = (
        sa.CheckConstraint(f"purpose IN {_in(MODEL_PURPOSES)}", name="purpose"),
        sa.Index("uq_model_assignment_current", "purpose", unique=True,
                 postgresql_where=sa.text("until IS NULL")),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    previous: Mapped[str | None] = mapped_column(sa.Text)
    since: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    rollback_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    trial_job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.job.id", ondelete="SET NULL"))
    forced: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    note: Mapped[str | None] = mapped_column(sa.Text)

