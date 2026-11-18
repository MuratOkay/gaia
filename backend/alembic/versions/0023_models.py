"""Models as managed things (Phase 7D; P7-4, P7-5; D101).

* ``ops.model``: a model registered beside the configured one — its URL, the id sent to the
  API, its key (the name of an environment variable, or encrypted with the master key), its
  locality (D24), options, and its probe;
* ``ops.model_assignment``: which model serves *reasoning* and *answering*, since when, the one
  it replaced and until when a rollback returns to it; one current row per purpose (none: the
  configured model);
* job kind ``model_trial`` and alert kind ``model_switched``.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
KINDS_OLD = "('refresh', 'drift_watch', 'sample')"
KINDS = "('refresh', 'drift_watch', 'sample', 'model_trial')"
ALERTS_OLD = "('job_failed', 'job_partial', 'drift', 'model_changed', 'worker_lost')"
ALERTS = ("('job_failed', 'job_partial', 'drift', 'model_changed', 'worker_lost', "
          "'model_switched')")


def _checks(kinds: str, alerts: str) -> None:
    for table in ("job", "schedule"):
        op.drop_constraint(f"ck_{table}_kind", table, schema="ops")
        op.create_check_constraint(op.f(f"ck_{table}_kind"), table, f"kind IN {kinds}",
                                   schema="ops")
    op.drop_constraint("ck_alert_kind", "alert", schema="ops")
    op.create_check_constraint(op.f("ck_alert_kind"), "alert", f"kind IN {alerts}",
                               schema="ops")


def upgrade() -> None:
    _checks(KINDS, ALERTS)
    op.create_table(
        "model",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("key_env", sa.Text(), nullable=True),
        sa.Column("secret", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.Text(), nullable=True),
        sa.Column("locality", sa.Text(), nullable=False),
        sa.Column("options", JSON, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("probe", JSON, nullable=True),
        sa.Column("probed_at", TS, nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", TS, nullable=True),
        sa.CheckConstraint("status IN ('unverified', 'verified', 'failed', 'retired')",
                           name=op.f("ck_model_status")),
        sa.CheckConstraint("locality IN ('local', 'remote')", name=op.f("ck_model_locality")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model")),
        sa.UniqueConstraint("name", name="uq_model_name"),
        schema="ops",
    )
    op.create_table(
        "model_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("previous", sa.Text(), nullable=True),
        sa.Column("since", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("until", TS, nullable=True),
        sa.Column("rollback_until", TS, nullable=True),
        sa.Column("trial_job_id", sa.Uuid(), nullable=True),
        sa.Column("forced", sa.Boolean(), nullable=False),
        sa.Column("by", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("purpose IN ('reasoning', 'answering')",
                           name=op.f("ck_model_assignment_purpose")),
        sa.ForeignKeyConstraint(["trial_job_id"], ["ops.job.id"], ondelete="SET NULL",
                                name=op.f("fk_model_assignment_trial_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_assignment")),
        schema="ops",
    )
    op.create_index("uq_model_assignment_current", "model_assignment", ["purpose"],
                    unique=True, schema="ops", postgresql_where=sa.text("until IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_model_assignment_current", table_name="model_assignment", schema="ops")
    op.drop_table("model_assignment", schema="ops")
    op.drop_table("model", schema="ops")
    op.execute("DELETE FROM ops.job WHERE kind = 'model_trial'")
    op.execute("DELETE FROM ops.schedule WHERE kind = 'model_trial'")
    op.execute("DELETE FROM ops.alert WHERE kind = 'model_switched'")
    _checks(KINDS_OLD, ALERTS_OLD)
