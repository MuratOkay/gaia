"""The calibration sample, and feedback set aside (Phase 7A; D96).

* ``semantics.review_sample`` (P7-2): meanings drawn each week per data source across the
  bands of the raw score — accepted ones included — and put before the SME blind; one row per
  column drawn, at most once ever;
* ``chat.feedback.voided_at`` / ``voided_by`` / ``void_note``: a "Was this right?" made in a
  test (or by mistake) set aside by an administrator — kept and shown, never a label.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "review_sample",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("column_semantic_id", sa.Uuid(), nullable=True),
        sa.Column("review_item_id", sa.Uuid(), nullable=True),
        sa.Column("band_from", sa.Float(), nullable=False),
        sa.Column("band_to", sa.Float(), nullable=False),
        sa.Column("raw_score", sa.Float(), nullable=False),
        sa.Column("drawn_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_review_sample_data_source_id_data_sources")),
        sa.ForeignKeyConstraint(["column_id"], ["catalog.columns.id"], ondelete="CASCADE",
                                name=op.f("fk_review_sample_column_id_columns")),
        sa.ForeignKeyConstraint(["column_semantic_id"], ["semantics.column_semantic.id"],
                                ondelete="SET NULL",
                                name=op.f("fk_review_sample_column_semantic_id_column_semantic")),
        sa.ForeignKeyConstraint(["review_item_id"], ["semantics.review_item.id"],
                                ondelete="SET NULL",
                                name=op.f("fk_review_sample_review_item_id_review_item")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_sample")),
        sa.UniqueConstraint("data_source_id", "column_id", name="uq_review_sample_column"),
        schema="semantics",
    )
    op.create_index("ix_review_sample_period", "review_sample", ["data_source_id", "period"],
                    schema="semantics")
    op.add_column("feedback", sa.Column("voided_at", TS, nullable=True), schema="chat")
    op.add_column("feedback", sa.Column("voided_by", sa.Text(), nullable=True), schema="chat")
    op.add_column("feedback", sa.Column("void_note", sa.Text(), nullable=True), schema="chat")


def downgrade() -> None:
    op.drop_column("feedback", "void_note", schema="chat")
    op.drop_column("feedback", "voided_by", schema="chat")
    op.drop_column("feedback", "voided_at", schema="chat")
    op.drop_index("ix_review_sample_period", table_name="review_sample", schema="semantics")
    op.drop_table("review_sample", schema="semantics")
