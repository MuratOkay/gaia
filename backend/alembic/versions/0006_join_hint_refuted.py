"""Join hints: status ``refuted`` (Phase 3A fixes, 2026-11-15).

A model-proposed join whose Stage 3 inclusion test of the claim found fewer
than half of the sampled rows in the target is ``refuted``; it is no longer
tested or offered as a join path, and a later passing test (``make
profile-tests ARGS=--force``) restores it. ``stale`` now also covers hints the
latest answer for a table no longer proposes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('confirmed', 'inferred', 'rejected', 'stale')"
NEW = "status IN ('confirmed', 'inferred', 'rejected', 'refuted', 'stale')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_join_hint_status"), "join_hint", schema="semantics", type_="check")
    op.create_check_constraint(op.f("ck_join_hint_status"), "join_hint", NEW, schema="semantics")


def downgrade() -> None:
    # Refuted hints become stale: neither is tested nor offered as a join path.
    op.execute("UPDATE semantics.join_hint SET status = 'stale' WHERE status = 'refuted'")
    op.drop_constraint(op.f("ck_join_hint_status"), "join_hint", schema="semantics", type_="check")
    op.create_check_constraint(op.f("ck_join_hint_status"), "join_hint", OLD, schema="semantics")
