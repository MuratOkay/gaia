"""Catalog models: current-state projection of a target schema.

Table names are **plural** (``tables``, ``columns``, ``constraints``, ...) on
purpose. ``table``, ``column`` and ``constraint`` are reserved words in
PostgreSQL, and unquoted identifiers would force quoting through every query
and migration. The schemas themselves follow OUTLINE.md §8.

Design: these tables hold the **current** state per data source, keyed by
``(data_source_id, schema_name, object_name)``. History is preserved in
``catalog.snapshots.document`` (full JSONB), so a query run can pin an exact
snapshot without duplicating every row on each capture.

``is_tombstoned`` marks objects that disappeared from the target. They are
never hard-deleted, so past query runs and semantic versions stay resolvable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gaia.db.models.base import Base


def new_uuid() -> uuid.UUID:
    """Primary key default.

    PostgreSQL 18 offers ``uuidv7()`` for time-ordered ids; we stay with
    ``uuid4`` here so the same models work on any PostgreSQL version.
    """
    return uuid.uuid4()


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_data_sources_name"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    dialect: Mapped[str] = mapped_column(sa.Text, nullable=False)
    database_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    server_version: Mapped[str | None] = mapped_column(sa.Text)
    # Credentials live in config/.env only — never in the metadata store.
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    snapshots: Mapped[list[SnapshotRecord]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )
    tables: Mapped[list[TableRecord]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )


class SnapshotRecord(Base):
    """One capture. ``document`` is the canonical snapshot JSON."""

    __tablename__ = "snapshots"
    __table_args__ = (
        sa.Index("ix_snapshots_data_source_captured", "data_source_id", "captured_at"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    server_version: Mapped[str | None] = mapped_column(sa.Text)
    table_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    view_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    column_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    data_source: Mapped[DataSource] = relationship(back_populates="snapshots")


class TableRecord(Base):
    """Current state of a table or view."""

    __tablename__ = "tables"
    __table_args__ = (
        sa.UniqueConstraint(
            "data_source_id", "schema_name", "object_name", name="uq_tables_source_object"
        ),
        sa.Index("ix_tables_source_schema", "data_source_id", "schema_name"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # table | view
    comment: Mapped[str | None] = mapped_column(sa.Text)

    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    column_structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Data-shape signals: refreshed on every capture, never part of the hash
    # inputs beyond ``data_hash`` itself.
    estimated_rows: Mapped[int | None] = mapped_column(sa.BigInteger)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    modification_counter: Mapped[int | None] = mapped_column(sa.BigInteger)
    stats_updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    partition_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    last_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    structure_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_tombstoned: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    data_source: Mapped[DataSource] = relationship(back_populates="tables")
    columns: Mapped[list[ColumnRecord]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    records: Mapped[list[ConstraintRecord]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    relations: Mapped[list[RelationRecord]] = relationship(
        back_populates="table",
        cascade="all, delete-orphan",
        foreign_keys="RelationRecord.table_id",
    )
    indexes: Mapped[list[IndexRecord]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )


class ColumnRecord(Base):
    """Current state of a column, with its own structure hash.

    The per-column hash is what allows surgical re-reasoning: changing one
    column in a 200-column table invalidates that column only.
    """

    __tablename__ = "columns"
    __table_args__ = (
        sa.UniqueConstraint(
            "table_id", "column_name", name="uq_columns_table_column"
        ),
        sa.Index("ix_columns_table", "table_id"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    data_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    nullable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    default_expression: Mapped[str | None] = mapped_column(sa.Text)
    comment: Mapped[str | None] = mapped_column(sa.Text)

    is_identity: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_computed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_sparse: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_rowguidcol: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_user_defined_type: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    max_length: Mapped[int | None] = mapped_column(sa.Integer)
    precision: Mapped[int | None] = mapped_column(sa.Integer)
    scale: Mapped[int | None] = mapped_column(sa.Integer)
    # Named ``collation_name``, not ``collation``: COLLATION is a reserved word
    # in PostgreSQL and unquoted use is a syntax error.
    collation_name: Mapped[str | None] = mapped_column(sa.Text)
    computed_definition: Mapped[str | None] = mapped_column(sa.Text)

    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    structure_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_tombstoned: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    table: Mapped[TableRecord] = relationship(back_populates="columns")



class ConstraintRecord(Base):
    """PK, unique and check constraints."""

    __tablename__ = "constraints"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "constraint_name", name="uq_constraints_table_name"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    constraint_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # primary_key|unique|check|default
    column_names: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    definition: Mapped[str | None] = mapped_column(sa.Text)
    is_disabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_not_trusted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)

    table: Mapped[TableRecord] = relationship(back_populates="records")


class RelationRecord(Base):
    """A foreign key edge. Composite order is preserved in both JSONB lists.

    ``is_not_trusted`` is tracked separately from ``is_disabled`` because a
    trusted FK guarantees existing rows were validated, which materially
    changes how much a join can be relied on.
    """

    __tablename__ = "relations"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "relation_name", name="uq_relations_table_name"),
        sa.Index("ix_relations_target", "to_schema", "to_table"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    relation_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_schema: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_table: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    to_schema: Mapped[str] = mapped_column(sa.Text, nullable=False)
    to_table: Mapped[str] = mapped_column(sa.Text, nullable=False)
    to_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    on_delete: Mapped[str | None] = mapped_column(sa.Text)
    on_update: Mapped[str | None] = mapped_column(sa.Text)
    is_disabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_not_trusted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)

    table: Mapped[TableRecord] = relationship(
        back_populates="relations", foreign_keys=[table_id]
    )


class IndexRecord(Base):
    __tablename__ = "indexes"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "index_name", name="uq_indexes_table_name"),
        {"schema": "catalog"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    index_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_primary_key: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_unique: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_unique_constraint: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    type_desc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    filter_definition: Mapped[str | None] = mapped_column(sa.Text)
    is_disabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    columns: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)

    table: Mapped[TableRecord] = relationship(back_populates="indexes")

