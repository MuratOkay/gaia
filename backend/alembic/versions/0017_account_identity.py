"""An account is the directory's object, not its login (D78).

``auth.account.pending_external_id`` and ``identity_changed_at``: when the directory's stable
id (AD ``objectGUID``, OpenLDAP ``entryUUID``, Entra's object id) behind a login differs from
the one the account belongs to, the new id is kept here, the sign-in is refused, and an
administrator re-links the account or starts it anew (its own grants revoked).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account", sa.Column("pending_external_id", sa.Text(), nullable=True),
                  schema="auth")
    op.add_column("account", sa.Column("identity_changed_at", sa.DateTime(timezone=True),
                                       nullable=True), schema="auth")


def downgrade() -> None:
    op.drop_column("account", "identity_changed_at", schema="auth")
    op.drop_column("account", "pending_external_id", schema="auth")
