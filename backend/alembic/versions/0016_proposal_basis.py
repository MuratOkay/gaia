"""What a category proposal rests on (D77): ``auth.object_category.basis``.

``direct`` (the object's schema, name or business name matches the category), ``view`` (a
view reading objects of one category), ``related`` (the table it is named after or
references), ``weak`` (only what Gaia wrote about it matches). NULL for decisions of
people and for proposals made before this revision.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("object_category", sa.Column("basis", sa.Text(), nullable=True),
                  schema="auth")
    op.create_check_constraint(
        op.f("ck_object_category_basis"), "object_category",
        "basis IS NULL OR basis IN ('direct', 'view', 'related', 'weak')", schema="auth")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_object_category_basis"), "object_category", schema="auth",
                       type_="check")
    op.drop_column("object_category", "basis", schema="auth")
