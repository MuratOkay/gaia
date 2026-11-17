"""The evaluation store (Phase 6A, D80): ``evaluation.run`` and ``evaluation.item``.

Every golden run is recorded — fresh, replayed, resumed (mixed) or imported from an older
run's files — with the build, the model, the question set and one item per question, so
``make quality`` can report over runs with intervals and compare them question by question.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evaluation")
    op.create_table(
        "run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data_source", sa.Text(), nullable=False),
        sa.Column("question_set", sa.Text(), nullable=True),
        sa.Column("question_set_key", sa.Text(), nullable=False),
        sa.Column("out_dir", sa.Text(), nullable=True),
        sa.Column("build", sa.Text(), nullable=True),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("as_login", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("live_calls", sa.Integer(), nullable=True),
        sa.Column("replayed_calls", sa.Integer(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint("kind IN ('golden')", name=op.f("ck_run_kind")),
        sa.CheckConstraint("mode IN ('fresh', 'replay', 'mixed', 'imported')",
                           name=op.f("ck_run_mode")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
        schema="evaluation",
    )
    op.create_index("ix_run_source_set", "run", ["data_source", "question_set_key",
                                                 "started_at"], schema="evaluation")
    op.create_table(
        "item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("expect", sa.Text(), nullable=True),
        sa.Column("first_status", sa.Text(), nullable=True),
        sa.Column("final_status", sa.Text(), nullable=True),
        sa.Column("correct", sa.Boolean(), nullable=True),
        sa.Column("correct_first", sa.Boolean(), nullable=True),
        sa.Column("executable", sa.Boolean(), nullable=True),
        sa.Column("over_clarified", sa.Boolean(), nullable=False),
        sa.Column("answered_directly", sa.Boolean(), nullable=False),
        sa.Column("best_guess", sa.Text(), nullable=True),
        sa.Column("ambiguity", sa.Text(), nullable=True),
        sa.Column("repairs", sa.Integer(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation.run.id"], ondelete="CASCADE",
                                name=op.f("fk_item_run_id_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item")),
        sa.UniqueConstraint("run_id", "item_key", name=op.f("uq_item_run_id_item_key")),
        schema="evaluation",
    )


def downgrade() -> None:
    op.drop_table("item", schema="evaluation")
    op.drop_index("ix_run_source_set", table_name="run", schema="evaluation")
    op.drop_table("run", schema="evaluation")
