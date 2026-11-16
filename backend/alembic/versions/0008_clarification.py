"""Clarifications (D61): every question Gaia asked back, its best guess, the user's choice.

Creates ``chat.clarification``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "chat"
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "clarification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("asked_by", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("clarification_question", sa.Text(), nullable=False),
        sa.Column("options", J, nullable=False),
        sa.Column("ambiguity", sa.Text(), nullable=True),
        sa.Column("default_option", sa.Integer(), nullable=True),
        sa.Column("best_guess_interpretation", sa.Text(), nullable=True),
        sa.Column("best_guess_assumptions", J, nullable=False),
        sa.Column("best_guess_sql", sa.Text(), nullable=True),
        sa.Column("best_guess_valid", sa.Boolean(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("chosen_option", sa.Integer(), nullable=True),
        sa.Column("took_default", sa.Boolean(), nullable=True),
        sa.Column("answer_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'answered', 'abandoned')",
                           name=op.f("ck_clarification_status")),
        sa.CheckConstraint("origin IN ('cli', 'api', 'golden', 'test')",
                           name=op.f("ck_clarification_origin")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                name=op.f("fk_clarification_data_source_id_data_sources"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["catalog.snapshots.id"],
                                name=op.f("fk_clarification_snapshot_id_snapshots"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["answer_run_id"], ["chat.query_run.id"],
                                name=op.f("fk_clarification_answer_run_id_query_run"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clarification")),
        schema=S,
    )
    op.create_index("ix_clarification_source_created", "clarification",
                    ["data_source_id", "created_at"], unique=False, schema=S)
    op.create_index("ix_clarification_status", "clarification", ["status"], unique=False,
                    schema=S)


def downgrade() -> None:
    op.drop_index("ix_clarification_status", table_name="clarification", schema=S)
    op.drop_index("ix_clarification_source_created", table_name="clarification", schema=S)
    op.drop_table("clarification", schema=S)
