"""The meanings a golden run answered with (D81): ``evaluation.run.meanings_key``, ``meanings``;
and each item's final SQL (``evaluation.item.sql``).

A fingerprint of the data source's current semantic versions, SME decisions, glossary terms and
confirmed joins when the run started, and their counts: the quality report warns when a
comparison's runs answered with different meanings (the D80 run credited an SME's decision to
the build). An item keeps the SQL of its final answer, so a question wrong in every run can be
read (the report shows the latest one).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("meanings_key", sa.Text(), nullable=True), schema="evaluation")
    op.add_column("run", sa.Column("meanings", postgresql.JSONB(astext_type=sa.Text()),
                                   nullable=True), schema="evaluation")
    op.add_column("item", sa.Column("sql", sa.Text(), nullable=True), schema="evaluation")


def downgrade() -> None:
    op.drop_column("item", "sql", schema="evaluation")
    op.drop_column("run", "meanings", schema="evaluation")
    op.drop_column("run", "meanings_key", schema="evaluation")
