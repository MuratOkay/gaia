"""Canonical snapshot document.

A snapshot is a self-contained, deterministic description of a target schema.
It is stored verbatim as JSONB **and** projected into normalized tables, so
nothing is lost that Gaia does not yet model.

Determinism matters more than brevity here: two snapshots of an unchanged
schema must serialize byte-identically, otherwise hashing and diffing produce
spurious churn. Every collection is therefore sorted by a stable key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gaia.introspect.hashing import normalize_text, structure_hash


@dataclass
class ColumnSnapshot:
    name: str
    ordinal: int
    data_type: str
    nullable: bool
    default: str | None = None
    comment: str | None = None
    is_identity: bool = False
    is_computed: bool = False
    is_sparse: bool = False
    is_rowguidcol: bool = False
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    collation: str | None = None
    is_user_defined_type: bool = False
    computed_definition: str | None = None
    # Definition of a user-defined type when the dialect exposes one, e.g. a
    # PostgreSQL enum ("enum ('new', 'paid')") or domain
    # ("domain crm.email_address: CHECK (...)"). SQL Server never sets it.
    # Included in the payloads only when set, so existing hashes are unchanged.
    type_definition: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "ordinal": self.ordinal,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "default": self.default,
            "comment": self.comment,
            "is_identity": self.is_identity,
            "is_computed": self.is_computed,
            "is_sparse": self.is_sparse,
            "is_rowguidcol": self.is_rowguidcol,
            "max_length": self.max_length,
            "precision": self.precision,
            "scale": self.scale,
            "collation": self.collation,
            "is_user_defined_type": self.is_user_defined_type,
            "computed_definition": self.computed_definition,
        }
        if self.type_definition is not None:
            data["type_definition"] = self.type_definition
        return data

    def structure_payload(self) -> dict[str, Any]:
        """Only semantically meaningful fields; ordinal is deliberately absent."""
        payload = {
            "name": self.name,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "default": normalize_text(self.default),
            "comment": normalize_text(self.comment),
            "is_identity": self.is_identity,
            "is_computed": self.is_computed,
            "is_sparse": self.is_sparse,
            "is_rowguidcol": self.is_rowguidcol,
            "max_length": self.max_length,
            "precision": self.precision,
            "scale": self.scale,
            "collation": self.collation,
            "is_user_defined_type": self.is_user_defined_type,
            "computed_definition": normalize_text(self.computed_definition),
        }
        if self.type_definition is not None:
            payload["type_definition"] = normalize_text(self.type_definition)
        return payload

    @property
    def structure_hash(self) -> str:
        return structure_hash(self.structure_payload())


@dataclass
class ConstraintSnapshot:
    name: str
    kind: str  # primary_key | unique | check | default | exclusion (PostgreSQL)
    columns: list[str] = field(default_factory=list)
    definition: str | None = None
    is_disabled: bool = False
    is_not_trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "columns": self.columns,
            "definition": self.definition,
            "is_disabled": self.is_disabled,
            "is_not_trusted": self.is_not_trusted,
        }

    def structure_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "columns": self.columns,
            "definition": normalize_text(self.definition),
            "is_disabled": self.is_disabled,
            "is_not_trusted": self.is_not_trusted,
        }


@dataclass
class RelationSnapshot:
    """A foreign key, with composite column order preserved.

    ``is_not_trusted`` is kept separate from ``is_disabled``: a trusted
    constraint is enforced for new rows *and* validated against existing data,
    which is a materially different guarantee when reasoning about data quality.
    """

    name: str
    from_schema: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]
    on_delete: str | None = None
    on_update: str | None = None
    is_disabled: bool = False
    is_not_trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "from_schema": self.from_schema,
            "from_table": self.from_table,
            "from_columns": self.from_columns,
            "to_schema": self.to_schema,
            "to_table": self.to_table,
            "to_columns": self.to_columns,
            "on_delete": self.on_delete,
            "on_update": self.on_update,
            "is_disabled": self.is_disabled,
            "is_not_trusted": self.is_not_trusted,
        }

    def structure_payload(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass
class IndexSnapshot:
    name: str
    is_primary_key: bool
    is_unique: bool
    is_unique_constraint: bool
    type_desc: str
    columns: list[dict[str, Any]] = field(default_factory=list)
    filter_definition: str | None = None
    is_disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_primary_key": self.is_primary_key,
            "is_unique": self.is_unique,
            "is_unique_constraint": self.is_unique_constraint,
            "type_desc": self.type_desc,
            "columns": self.columns,
            "filter_definition": self.filter_definition,
            "is_disabled": self.is_disabled,
        }

    def structure_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_primary_key": self.is_primary_key,
            "is_unique": self.is_unique,
            "is_unique_constraint": self.is_unique_constraint,
            "type_desc": self.type_desc,
            "columns": self.columns,
            "filter_definition": normalize_text(self.filter_definition),
            "is_disabled": self.is_disabled,
        }


@dataclass
class TableSnapshot:
    schema: str
    name: str
    kind: str  # table | view (PostgreSQL materialized views are reported as view)
    comment: str | None = None
    estimated_rows: int | None = None
    size_bytes: int | None = None
    modification_counter: int | None = None
    stats_updated_at: datetime | None = None
    columns: list[ColumnSnapshot] = field(default_factory=list)
    constraints: list[ConstraintSnapshot] = field(default_factory=list)
    relations: list[RelationSnapshot] = field(default_factory=list)
    indexes: list[IndexSnapshot] = field(default_factory=list)
    partition_count: int = 1

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def structure_hash(self) -> str:
        return structure_hash(self.structure_payload())

    @property
    def data_hash(self) -> str:
        from gaia.introspect.hashing import data_hash as _data_hash

        return _data_hash(self.data_payload())

    def structure_payload(self) -> dict[str, Any]:
        """Semantic structure only — never rows, sizes or stats freshness.

        Columns are sorted by name, not ordinal, so reordering a table's columns
        is not treated as a structural change: it carries no meaning for
        natural-language querying but would otherwise cost a full re-reasoning.
        """
        return {
            "schema": self.schema,
            "name": self.name,
            "kind": self.kind,
            "comment": normalize_text(self.comment),
            "columns": [
                c.structure_payload() for c in sorted(self.columns, key=lambda c: c.name)
            ],
            "constraints": [
                c.structure_payload()
                for c in sorted(self.constraints, key=lambda c: (c.kind, c.name))
            ],
            "relations": [
                r.structure_payload() for r in sorted(self.relations, key=lambda r: r.name)
            ],
            "indexes": [
                i.structure_payload() for i in sorted(self.indexes, key=lambda i: i.name)
            ],
        }

    def data_payload(self) -> dict[str, Any]:
        """Data-shape signals: changes here invalidate profiles, not semantics."""
        return {
            "estimated_rows": self.estimated_rows,
            "modification_counter": self.modification_counter,
            "stats_updated_at": (
                self.stats_updated_at.isoformat() if self.stats_updated_at else None
            ),
        }

    def sorted_view(self) -> TableSnapshot:
        """Return a copy with every collection in canonical order."""
        return TableSnapshot(
            schema=self.schema,
            name=self.name,
            kind=self.kind,
            comment=self.comment,
            estimated_rows=self.estimated_rows,
            size_bytes=self.size_bytes,
            modification_counter=self.modification_counter,
            stats_updated_at=self.stats_updated_at,
            columns=sorted(self.columns, key=lambda c: c.ordinal),
            constraints=sorted(self.constraints, key=lambda c: (c.kind, c.name)),
            relations=sorted(self.relations, key=lambda r: r.name),
            indexes=sorted(self.indexes, key=lambda i: i.name),
            partition_count=self.partition_count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "kind": self.kind,
            "comment": self.comment,
            "estimated_rows": self.estimated_rows,
            "size_bytes": self.size_bytes,
            "modification_counter": self.modification_counter,
            "stats_updated_at": (
                self.stats_updated_at.isoformat() if self.stats_updated_at else None
            ),
            "partition_count": self.partition_count,
            "columns": [c.to_dict() for c in self.columns],
            "constraints": [c.to_dict() for c in self.constraints],
            "relations": [r.to_dict() for r in self.relations],
            "indexes": [i.to_dict() for i in self.indexes],
        }


@dataclass
class Snapshot:
    data_source: str
    dialect: str
    database: str
    server_version: str
    tables: list[TableSnapshot] = field(default_factory=list)
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def sorted_view(self) -> Snapshot:
        return Snapshot(
            data_source=self.data_source,
            dialect=self.dialect,
            database=self.database,
            server_version=self.server_version,
            tables=sorted(
                (t.sorted_view() for t in self.tables), key=lambda t: (t.schema, t.name)
            ),
            snapshot_id=self.snapshot_id,
            captured_at=self.captured_at,
        )

    @property
    def structure_hash(self) -> str:
        return structure_hash(
            {
                "dialect": self.dialect,
                "database": self.database,
                "tables": [t.structure_payload() for t in self.tables],
            }
        )

    @property
    def data_hash(self) -> str:
        from gaia.introspect.hashing import data_hash as _data_hash

        return _data_hash({"tables": [t.data_payload() for t in self.tables]})

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    def table(self, schema: str, name: str) -> TableSnapshot | None:
        for t in self.tables:
            if t.schema == schema and t.name == name:
                return t
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "data_source": self.data_source,
            "dialect": self.dialect,
            "database": self.database,
            "server_version": self.server_version,
            "captured_at": self.captured_at.isoformat(),
            "structure_hash": self.structure_hash,
            "data_hash": self.data_hash,
            "counts": {
                "tables": sum(1 for t in self.tables if t.kind == "table"),
                "views": sum(1 for t in self.tables if t.kind == "view"),
                "columns": self.column_count,
            },
            "tables": [t.to_dict() for t in self.tables],
        }


def finalize(snapshot: Snapshot) -> Snapshot:
    """Canonicalize before hashing or serializing."""
    return snapshot.sorted_view()


