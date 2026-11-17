"""Clarification options in a shuffled, stored order (Phase 5, step 5.0; triage T5).

The model listed its own choice first in 13 of 13 clarifications on the host, so the
order it wrote gave Gaia's choice away and would inflate D61's agreement measurement.
Options are now shown in a shuffled order; ``options`` and ``default_option`` keep
the order *shown*, and ``option_order`` the model's index of each shown option, so
position effects stay measurable. NULL on earlier rows: shown as the model wrote them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clarification",
                  sa.Column("option_order", postgresql.JSONB(astext_type=sa.Text()),
                            nullable=True),
                  schema="chat")


def downgrade() -> None:
    op.drop_column("clarification", "option_order", schema="chat")
