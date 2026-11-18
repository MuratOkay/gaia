"""Operations: jobs, schedules, their log, and alerts (Phase 7C; P7-6, P7-7; D98).

Schema ``ops``: ``schedule`` (a kind of job queued by itself, daily or weekly), ``job`` (one
piece of work on a data source, claimed by the worker with ``FOR UPDATE SKIP LOCKED``; at most
one queued or running per kind and data source), ``job_event`` (its log) and ``alert`` (what
an administrator should look at, until acknowledged). The schema is left in place on
downgrade (empty), like the other schemas.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
KINDS = "('refresh', 'drift_watch', 'sample')"
STATES = "('queued', 'running', 'done', 'partial', 'cancelled', 'failed', 'refused')"
ALERTS = "('job_failed', 'job_partial', 'drift', 'model_changed', 'worker_lost')"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ops")
    op.create_table(
        "schedule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("every", sa.Text(), nullable=False),
        sa.Column("at_time", sa.Text(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("params", JSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_due_at", TS, nullable=False),
        sa.Column("last_queued_at", TS, nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"kind IN {KINDS}", name=op.f("ck_schedule_kind")),
        sa.CheckConstraint("every IN ('daily', 'weekly')", name=op.f("ck_schedule_every")),
        sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6",
                           name=op.f("ck_schedule_weekday")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
        sa.UniqueConstraint("data_source", "kind", name="uq_schedule_source_kind"),
        schema="ops",
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data_source", sa.Text(), nullable=False),
        sa.Column("params", JSON, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("worker", sa.Text(), nullable=True),
        sa.Column("progress", JSON, nullable=False),
        sa.Column("result", JSON, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", TS, nullable=True),
        sa.Column("cancel_requested_by", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("heartbeat_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(f"kind IN {KINDS}", name=op.f("ck_job_kind")),
        sa.CheckConstraint(f"state IN {STATES}", name=op.f("ck_job_state")),
        sa.ForeignKeyConstraint(["schedule_id"], ["ops.schedule.id"], ondelete="SET NULL",
                                name=op.f("fk_job_schedule_id_schedule")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
        schema="ops",
    )
    op.create_index("ix_job_state_created", "job", ["state", "created_at"], schema="ops")
    op.create_index("ix_job_source_created", "job", ["data_source", "created_at"], schema="ops")
    op.create_index("uq_job_active", "job", ["data_source", "kind"], unique=True, schema="ops",
                    postgresql_where=sa.text("state IN ('queued', 'running')"))
    op.create_table(
        "job_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["ops.job.id"], ondelete="CASCADE",
                                name=op.f("fk_job_event_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_event")),
        schema="ops",
    )
    op.create_index("ix_job_event_job", "job_event", ["job_id", "seq"], schema="ops")
    op.create_table(
        "alert",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data_source", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", JSON, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", TS, nullable=True),
        sa.Column("acknowledged_by", sa.Text(), nullable=True),
        sa.CheckConstraint(f"kind IN {ALERTS}", name=op.f("ck_alert_kind")),
        sa.ForeignKeyConstraint(["job_id"], ["ops.job.id"], ondelete="SET NULL",
                                name=op.f("fk_alert_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert")),
        schema="ops",
    )
    op.create_index("ix_alert_open", "alert", ["acknowledged_at", "created_at"], schema="ops")


def downgrade() -> None:
    op.drop_index("ix_alert_open", table_name="alert", schema="ops")
    op.drop_table("alert", schema="ops")
    op.drop_index("ix_job_event_job", table_name="job_event", schema="ops")
    op.drop_table("job_event", schema="ops")
    op.drop_index("uq_job_active", table_name="job", schema="ops")
    op.drop_index("ix_job_source_created", table_name="job", schema="ops")
    op.drop_index("ix_job_state_created", table_name="job", schema="ops")
    op.drop_table("job", schema="ops")
    op.drop_table("schedule", schema="ops")
