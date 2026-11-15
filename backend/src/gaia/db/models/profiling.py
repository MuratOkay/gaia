"""Profiling models (Phase 3A): runs, table/column profiles, tests, modules, lineage.

Profiles are *versioned by currency*, like semantics: a new profile of a table
clears ``is_current`` on the previous one, which is kept for history. Every
column metric records how it was obtained (``metrics``: method, rows scanned,
pages, runtime, seed, isolation, error or skip reason), so a number that looks
wrong can always be traced to its query.

Sample rows are never stored; ``column_profile.summary`` holds aggregates and at
most a handful of example values (PHASES P3-7: examples are masked when they go
to a remote model, not when they are stored — data at rest is backlog).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gaia.db.models.base import Base
from gaia.db.models.catalog import new_uuid

__all__ = [
    "ColumnProfile",
    "ModuleDefinition",
    "ProfileRun",
    "TableProfile",
    "VerificationTest",
    "ViewColumnLineage",
]

TABLE_PROFILE_STATUSES = ("profiled", "partial", "skipped", "failed")


class ProfileRun(Base):
    __tablename__ = "profile_run"
    __table_args__ = (
        sa.Index("ix_profile_run_source_started", "data_source_id", "started_at"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_failures', 'failed')",
            name="status",
        ),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(sa.Text, nullable=False)  # "1" | "3"
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    tables_in_scope: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_profiled: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_reused: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_skipped: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    queries: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    pages_estimated: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    wall_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    footprint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class TableProfile(Base):
    __tablename__ = "table_profile"
    __table_args__ = (
        sa.Index(
            "uq_table_profile_current",
            "table_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_table_profile_source_current", "data_source_id", "is_current"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in TABLE_PROFILE_STATUSES) + ")",
            name="status",
        ),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("profiling.profile_run.id", ondelete="SET NULL")
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(sa.Text)
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Catalog row estimate when profiled: the freshness policy compares to it.
    rows_at_profile: Mapped[int | None] = mapped_column(sa.BigInteger)
    row_count: Mapped[int | None] = mapped_column(sa.BigInteger)
    size_class: Mapped[str | None] = mapped_column(sa.Text)
    pages_estimated: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    runtime_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    queries: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    isolation: Mapped[str | None] = mapped_column(sa.Text)
    seed: Mapped[int | None] = mapped_column(sa.Integer)
    # Table-level evidence: identities, view aliases of its columns, lineage (views).
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    stage3_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    profiled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ColumnProfile(Base):
    __tablename__ = "column_profile"
    __table_args__ = (
        sa.Index("ix_column_profile_table_profile", "table_profile_id"),
        sa.Index("ix_column_profile_column", "column_id"),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_profile_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("profiling.table_profile.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.columns.id", ondelete="CASCADE")
    )
    column_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class VerificationTest(Base):
    """A test of a column: Stage 1 join candidates, Stage 3 checks of claims."""

    __tablename__ = "verification_test"
    __table_args__ = (
        sa.Index(
            "uq_verification_test_current",
            "table_id",
            "column_name",
            "test_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_verification_test_column", "column_id", "is_current"),
        sa.CheckConstraint("origin IN ('candidate', 'claim')", name="origin"),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.columns.id", ondelete="CASCADE")
    )
    column_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    test_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target: Mapped[str | None] = mapped_column(sa.Text)
    stage: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)  # candidate | claim
    specific: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    passed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    metric: Mapped[float | None] = mapped_column(sa.Float)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    method: Mapped[str | None] = mapped_column(sa.Text)
    runtime_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ModuleDefinition(Base):
    """View / materialized view / procedure / function / trigger source text."""

    __tablename__ = "module_definition"
    __table_args__ = (
        sa.Index(
            "uq_module_definition_current",
            "data_source_id",
            "schema_name",
            "object_name",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    definition: Mapped[str | None] = mapped_column(sa.Text)
    definition_hash: Mapped[str | None] = mapped_column(sa.Text)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    captured_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ViewColumnLineage(Base):
    __tablename__ = "view_column_lineage"
    __table_args__ = (
        sa.Index("ix_view_column_lineage_module", "module_definition_id"),
        sa.Index("ix_view_column_lineage_source", "data_source_id", "source"),
        sa.CheckConstraint(
            "classification IN ('pass_through', 'computed', 'unresolved')", name="classification"
        ),
        {"schema": "profiling"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    module_definition_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("profiling.module_definition.id", ondelete="CASCADE"), nullable=False
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    view_schema: Mapped[str] = mapped_column(sa.Text, nullable=False)
    view_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    column_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    classification: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str | None] = mapped_column(sa.Text)  # schema.table.column
    expression: Mapped[str | None] = mapped_column(sa.Text)
    inputs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # computed, but only a conversion of this one column (cast, to_date, ...)
    converts: Mapped[str | None] = mapped_column(sa.Text)
    detail: Mapped[str | None] = mapped_column(sa.Text)
