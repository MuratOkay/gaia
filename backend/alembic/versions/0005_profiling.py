"""Profiling (Phase 3A): runs, table/column profiles, tests, modules, view lineage.

Creates in ``profiling``: ``profile_run``, ``table_profile``, ``column_profile``,
``verification_test``, ``module_definition``, ``view_column_lineage``.

Module definitions live here, not in the catalog snapshot, so capturing them
changes no structure hash and re-baselines nothing; a definition change reaches
reasoning through the evidence hash (lineage is part of the evidence block).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "profiling"
NOW = sa.text("now()")


def _uuid(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS profiling")
    op.create_table(
        "profile_run",
        _uuid("id"), _uuid("data_source_id"), _uuid("snapshot_id", True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tables_in_scope", sa.Integer(), nullable=False),
        sa.Column("tables_profiled", sa.Integer(), nullable=False),
        sa.Column("tables_reused", sa.Integer(), nullable=False),
        sa.Column("tables_skipped", sa.Integer(), nullable=False),
        sa.Column("tables_failed", sa.Integer(), nullable=False),
        sa.Column("queries", sa.Integer(), nullable=False),
        sa.Column("pages_estimated", sa.BigInteger(), nullable=False),
        sa.Column("wall_ms", sa.BigInteger(), nullable=True),
        sa.Column("footprint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_failures', 'failed')",
            name="status"),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["catalog.snapshots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("ix_profile_run_source_started", "profile_run",
                    ["data_source_id", "started_at"], unique=False, schema=S)

    op.create_table(
        "table_profile",
        _uuid("id"), _uuid("run_id", True), _uuid("data_source_id"), _uuid("table_id"),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("structure_hash", sa.Text(), nullable=False),
        sa.Column("rows_at_profile", sa.BigInteger(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("size_class", sa.Text(), nullable=True),
        sa.Column("pages_estimated", sa.BigInteger(), nullable=False),
        sa.Column("runtime_ms", sa.Integer(), nullable=False),
        sa.Column("queries", sa.Integer(), nullable=False),
        sa.Column("isolation", sa.Text(), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stage3_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profiled_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("status IN ('profiled', 'partial', 'skipped', 'failed')",
                           name="status"),
        sa.ForeignKeyConstraint(["run_id"], ["profiling.profile_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("uq_table_profile_current", "table_profile", ["table_id"], unique=True,
                    schema=S, postgresql_where=sa.text("is_current"))
    op.create_index("ix_table_profile_source_current", "table_profile",
                    ["data_source_id", "is_current"], unique=False, schema=S)

    op.create_table(
        "column_profile",
        _uuid("id"), _uuid("table_profile_id"), _uuid("table_id"), _uuid("column_id", True),
        sa.Column("column_name", sa.Text(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["table_profile_id"], ["profiling.table_profile.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["catalog.columns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("ix_column_profile_table_profile", "column_profile", ["table_profile_id"],
                    unique=False, schema=S)
    op.create_index("ix_column_profile_column", "column_profile", ["column_id"], unique=False,
                    schema=S)

    op.create_table(
        "verification_test",
        _uuid("id"), _uuid("data_source_id"), _uuid("table_id"), _uuid("column_id", True),
        sa.Column("column_name", sa.Text(), nullable=False),
        sa.Column("test_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("specific", sa.Boolean(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("metric", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("runtime_ms", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["catalog.columns.id"], ondelete="CASCADE"),
        sa.CheckConstraint("origin IN ('candidate', 'claim')", name="origin"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("uq_verification_test_current", "verification_test",
                    ["table_id", "column_name", "test_id"], unique=True, schema=S, postgresql_where=sa.text("is_current"))
    op.create_index("ix_verification_test_column", "verification_test",
                    ["column_id", "is_current"], unique=False, schema=S)

    op.create_table(
        "module_definition",
        _uuid("id"), _uuid("data_source_id"),
        sa.Column("schema_name", sa.Text(), nullable=False),
        sa.Column("object_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("definition_hash", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("uq_module_definition_current", "module_definition",
                    ["data_source_id", "schema_name", "object_name"], unique=True, schema=S,
                    postgresql_where=sa.text("is_current"))

    op.create_table(
        "view_column_lineage",
        _uuid("id"), _uuid("module_definition_id"), _uuid("data_source_id"),
        sa.Column("view_schema", sa.Text(), nullable=False),
        sa.Column("view_name", sa.Text(), nullable=False),
        sa.Column("column_name", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("expression", sa.Text(), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("converts", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("classification IN ('pass_through', 'computed', 'unresolved')",
                           name="classification"),
        sa.ForeignKeyConstraint(["module_definition_id"], ["profiling.module_definition.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=S,
    )
    op.create_index("ix_view_column_lineage_module", "view_column_lineage",
                    ["module_definition_id"], unique=False, schema=S)
    op.create_index("ix_view_column_lineage_source", "view_column_lineage",
                    ["data_source_id", "source"], unique=False, schema=S)


def downgrade() -> None:
    for table in ("view_column_lineage", "module_definition", "verification_test",
                  "column_profile", "table_profile", "profile_run"):
        op.drop_table(table, schema=S)
