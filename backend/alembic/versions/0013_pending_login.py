"""Sign-in with Microsoft Entra ID (Phase 5A.2): ``auth.pending_login``.

One row per OpenID Connect sign-in started and not yet finished: the hashes of its
``state`` and of the browser binding, the nonce, the PKCE verifier encrypted with the
master key; single use, short-lived.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "pending_login",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("directory_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("verifier", sa.LargeBinary(), nullable=False),
        sa.Column("verifier_key_id", sa.Text(), nullable=False),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["directory_id"], ["auth.directory.id"], ondelete="CASCADE",
                                name=op.f("fk_pending_login_directory_id_directory")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_login")),
        sa.UniqueConstraint("state_hash", name="uq_pending_login_state"),
        schema="auth",
    )
    op.create_index("ix_pending_login_expires", "pending_login", ["expires_at"], schema="auth")


def downgrade() -> None:
    op.drop_table("pending_login", schema="auth")
