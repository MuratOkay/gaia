"""SME review (Phase 5B, D73): who decided a meaning, when, and why.

``semantics.table_semantic`` and ``semantics.column_semantic`` gain ``reviewed_by``,
``reviewed_at`` and ``review_note``. An SME's decision is a new version with status
``sme_reviewed`` or ``sme_rejected``; re-reasoning keeps it current while the column's
structure is unchanged (D16), and marks it ``stale`` — still signed — when it changed.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("table_semantic", "column_semantic")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("reviewed_by", sa.Text(), nullable=True), schema="semantics")
        op.add_column(table, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
                      schema="semantics")
        op.add_column(table, sa.Column("review_note", sa.Text(), nullable=True), schema="semantics")


def downgrade() -> None:
    for table in TABLES:
        for column in ("review_note", "reviewed_at", "reviewed_by"):
            op.drop_column(table, column, schema="semantics")
