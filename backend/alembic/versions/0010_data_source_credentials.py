"""Data sources added at runtime (Phase 4F): encrypted credentials, status, audit.

``catalog.data_sources`` gains origin, status, connection settings, the encrypted
secret and its key id, the last probe and a tombstone time; existing rows become
origin/status ``config`` (config/gaia.yaml targets, unchanged). Creates
``audit.connection_change``.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "catalog"
J = postgresql.JSONB(astext_type=sa.Text())
STATUS = "status IN ('config', 'unverified', 'verified', 'failed', 'disabled', 'tombstoned')"


def upgrade() -> None:
    op.add_column("data_sources", sa.Column("origin", sa.Text(), server_default="config",
                                            nullable=False), schema=C)
    op.add_column("data_sources", sa.Column("status", sa.Text(), server_default="config",
                                            nullable=False), schema=C)
    op.add_column("data_sources", sa.Column("connection", J, nullable=True), schema=C)
    op.add_column("data_sources", sa.Column("secret", sa.LargeBinary(), nullable=True), schema=C)
    op.add_column("data_sources", sa.Column("secret_key_id", sa.Text(), nullable=True), schema=C)
    op.add_column("data_sources", sa.Column("probed_at", sa.DateTime(timezone=True),
                                            nullable=True), schema=C)
    op.add_column("data_sources", sa.Column("probe", J, nullable=True), schema=C)
    op.add_column("data_sources", sa.Column("tombstoned_at", sa.DateTime(timezone=True),
                                            nullable=True), schema=C)
    op.create_check_constraint(op.f("ck_data_sources_origin"), "data_sources",
                               "origin IN ('config', 'admin')", schema=C)
    op.create_check_constraint(op.f("ck_data_sources_status"), "data_sources", STATUS, schema=C)
    op.create_table(
        "connection_change",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("data_source_name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("detail", J, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'probed', 'disabled', 'enabled', 'removed', "
            "'rotated')", name=op.f("ck_connection_change_action")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                name=op.f("fk_connection_change_data_source_id_data_sources"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection_change")),
        schema="audit",
    )
    op.create_index("ix_connection_change_source", "connection_change",
                    ["data_source_id", "created_at"], unique=False, schema="audit")


def downgrade() -> None:
    # Admin-added data sources exist only in the database: they lose their credentials.
    op.drop_index("ix_connection_change_source", table_name="connection_change",
                  schema="audit")
    op.drop_table("connection_change", schema="audit")
    op.drop_constraint(op.f("ck_data_sources_status"), "data_sources", schema=C, type_="check")
    op.drop_constraint(op.f("ck_data_sources_origin"), "data_sources", schema=C, type_="check")
    for column in ("tombstoned_at", "probe", "probed_at", "secret_key_id", "secret",
                   "connection", "status", "origin"):
        op.drop_column("data_sources", column, schema=C)
