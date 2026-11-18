"""What an answer was made of, people's labels, fitted calibrators, and feedback on answers
(D84–D87).

* ``evaluation.item.detail`` (D84): the context's objects and the expected objects it lacked,
  the interpretation, assumptions, clarification and confidence of the final answer — a
  question wrong in every run now says whether retrieval or the model missed;
* ``evaluation.label`` (6B part 2, D85): people's verdicts kept nowhere else — the owner's 3C
  audit, with the judge's verdict beside it;
* ``evaluation.calibrator`` (6C, D86): a fitted map from raw score to P(right) per kind, model
  and scoring version, its held-out metrics, and whether it is in use (one at most);
* ``chat.feedback`` (6D, D87): "Was this right?" under an answer, and the SME's decision on a
  "partly" or "wrong".

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("item", sa.Column("detail", JSON, nullable=True), schema="evaluation")

    op.create_table(
        "label",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("data_source", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("judge_verdict", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("production", sa.Boolean(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("scoring", sa.Text(), nullable=True),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("reviewer", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("verdict IN ('right', 'partly', 'wrong')",
                           name=op.f("ck_label_verdict")),
        sa.CheckConstraint("judge_verdict IS NULL OR judge_verdict IN ('right', 'partly', "
                           "'wrong')", name=op.f("ck_label_judge_verdict")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label")),
        sa.UniqueConstraint("source", "origin", name=op.f("uq_label_source_origin")),
        schema="evaluation",
    )
    op.create_index("ix_label_source_ds", "label", ["data_source", "source"],
                    schema="evaluation")

    op.create_table(
        "calibrator",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("scoring", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("params", JSON, nullable=False),
        sa.Column("metrics", JSON, nullable=False),
        sa.Column("labels", sa.Integer(), nullable=False),
        sa.Column("labels_wrong", sa.Integer(), nullable=False),
        sa.Column("labels_key", sa.Text(), nullable=False),
        sa.Column("in_use", sa.Boolean(), nullable=False),
        sa.Column("fitted_by", sa.Text(), nullable=True),
        sa.Column("fitted_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("applied_at", TS, nullable=True),
        sa.CheckConstraint("kind IN ('meaning', 'answer')", name=op.f("ck_calibrator_kind")),
        sa.CheckConstraint("method IN ('identity', 'platt', 'isotonic')",
                           name=op.f("ck_calibrator_method")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibrator")),
        sa.UniqueConstraint("version", name=op.f("uq_calibrator_version")),
        schema="evaluation",
    )
    op.create_index("uq_calibrator_in_use", "calibrator", ["kind", "model", "scoring"],
                    unique=True, schema="evaluation", postgresql_where=sa.text("in_use"))

    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("given_by", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("verdict IN ('right', 'partly', 'wrong')",
                           name=op.f("ck_feedback_verdict")),
        sa.CheckConstraint("status IN ('none', 'open', 'decided')",
                           name=op.f("ck_feedback_status")),
        sa.CheckConstraint("decision IS NULL OR decision IN ('right', 'partly', 'wrong')",
                           name=op.f("ck_feedback_decision")),
        sa.CheckConstraint("cause IS NULL OR cause IN ('meaning', 'definition', 'join', "
                           "'model', 'data', 'question', 'other')",
                           name=op.f("ck_feedback_cause")),
        sa.ForeignKeyConstraint(["message_id"], ["chat.message.id"], ondelete="CASCADE",
                                name=op.f("fk_feedback_message_id_message")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_feedback_data_source_id_data_sources")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("message_id", "given_by", name="uq_feedback_message_person"),
        schema="chat",
    )
    op.create_index("ix_feedback_source_status", "feedback",
                    ["data_source_id", "status", "created_at"], schema="chat")


def downgrade() -> None:
    op.drop_index("ix_feedback_source_status", table_name="feedback", schema="chat")
    op.drop_table("feedback", schema="chat")
    op.drop_index("uq_calibrator_in_use", table_name="calibrator", schema="evaluation")
    op.drop_table("calibrator", schema="evaluation")
    op.drop_index("ix_label_source_ds", table_name="label", schema="evaluation")
    op.drop_table("label", schema="evaluation")
    op.drop_column("item", "detail", schema="evaluation")
