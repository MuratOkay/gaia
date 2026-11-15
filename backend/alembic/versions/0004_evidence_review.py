"""Evidence ledger and review queue (Phase 3B).

* ``semantics.table_semantic``: ``evidence_hash`` (part of the skip key),
  ``exposure`` (raw | masked), ``chunks``.
* ``semantics.column_semantic``: ``semantic_type``, ``claimed_semantic_type``,
  ``coverage``, ``unused``.
* ``semantics.evidence_item``: the heuristic-v2 ledger of every scored version.
* ``semantics.review_item``: the SME review queue (one open item per column).

Migration numbers are assigned at creation (PHASES R4); 3B ships before 3A, so
it takes ``0004`` and 3A's profiling tables take the next number.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COVERAGE = "coverage IN ('catalog', 'modules', 'light_profile', 'targeted', 'sme')"


def upgrade() -> None:
    op.add_column("table_semantic", sa.Column("evidence_hash", sa.Text(), nullable=True),
                  schema="semantics")
    op.add_column("table_semantic", sa.Column("exposure", sa.Text(), nullable=True),
                  schema="semantics")
    op.add_column("table_semantic",
                  sa.Column("chunks", sa.Integer(), server_default="1", nullable=False),
                  schema="semantics")

    op.add_column("column_semantic", sa.Column("semantic_type", sa.Text(), nullable=True),
                  schema="semantics")
    op.add_column("column_semantic", sa.Column("claimed_semantic_type", sa.Text(), nullable=True),
                  schema="semantics")
    op.add_column("column_semantic",
                  sa.Column("coverage", sa.Text(), server_default="catalog", nullable=False),
                  schema="semantics")
    op.add_column("column_semantic",
                  sa.Column("unused", sa.Boolean(), server_default=sa.false(), nullable=False),
                  schema="semantics")
    op.create_check_constraint("coverage", "column_semantic", COVERAGE, schema="semantics")

    op.create_table(
        "evidence_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("column_semantic_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("direction IN ('supports', 'contradicts')", name="direction"),
        sa.CheckConstraint(
            "source IN ('catalog', 'module', 'profile', 'detector', 'identity', 'test', 'sme')",
            name="source",
        ),
        sa.ForeignKeyConstraint(["column_semantic_id"], ["semantics.column_semantic.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="semantics",
    )
    op.create_index("ix_evidence_item_column_semantic", "evidence_item", ["column_semantic_id"],
                    unique=False, schema="semantics")
    op.create_index("ix_evidence_item_kind", "evidence_item", ["kind", "direction"],
                    unique=False, schema="semantics")

    op.create_table(
        "review_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=True),
        sa.Column("column_semantic_id", sa.Uuid(), nullable=True),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'done', 'dismissed', 'superseded')",
                           name="status"),
        sa.CheckConstraint("subject_kind IN ('column', 'table')", name="subject_kind"),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["catalog.columns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_semantic_id"], ["semantics.column_semantic.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="semantics",
    )
    op.create_index("uq_review_item_open_column", "review_item", ["column_id"], unique=True,
                    schema="semantics", postgresql_where=sa.text("status = 'open'"))
    op.create_index("ix_review_item_queue", "review_item",
                    ["data_source_id", "status", "priority"], unique=False, schema="semantics")


def downgrade() -> None:
    op.drop_index("ix_review_item_queue", table_name="review_item", schema="semantics")
    op.drop_index("uq_review_item_open_column", table_name="review_item", schema="semantics",
                  postgresql_where=sa.text("status = 'open'"))
    op.drop_table("review_item", schema="semantics")
    op.drop_index("ix_evidence_item_kind", table_name="evidence_item", schema="semantics")
    op.drop_index("ix_evidence_item_column_semantic", table_name="evidence_item",
                  schema="semantics")
    op.drop_table("evidence_item", schema="semantics")
    op.drop_constraint("coverage", "column_semantic", schema="semantics", type_="check")
    for column in ("unused", "coverage", "claimed_semantic_type", "semantic_type"):
        op.drop_column("column_semantic", column, schema="semantics")
    for column in ("chunks", "exposure", "evidence_hash"):
        op.drop_column("table_semantic", column, schema="semantics")
