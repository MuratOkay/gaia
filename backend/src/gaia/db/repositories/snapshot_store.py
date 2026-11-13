"""Persists snapshots and their diffs into the metadata store.

Two responsibilities, kept together because they must be atomic:

1. Append the snapshot document to ``catalog.snapshots`` (the history).
2. Upsert the current state into ``catalog.tables``/``columns``/... while
   tombstoning anything that disappeared.

Tombstoning rather than deleting is deliberate: a query run pins a snapshot id
and a semantic version, and those must stay resolvable after a table is dropped
from the target.

Only rows whose hashes actually changed are written, so re-running
introspection against an unchanged schema is cheap and produces no churn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import (
    ColumnRecord,
    ConstraintRecord,
    DataSource,
    IndexRecord,
    RelationRecord,
    SnapshotRecord,
    TableRecord,
)
from gaia.introspect.diff import DiffResult
from gaia.introspect.hashing import structure_hash
from gaia.introspect.snapshot import Snapshot, TableSnapshot

logger = logging.getLogger(__name__)

__all__ = ["ApplyResult", "SnapshotStore"]


@dataclass
class ApplyResult:
    snapshot_id: str
    data_source_id: str
    tables_written: int = 0
    tables_unchanged: int = 0
    tables_tombstoned: int = 0
    columns_written: int = 0
    relations_written: int = 0
    diff: DiffResult | None = None
    notes: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(payload: dict) -> str:
    return structure_hash(payload)


class SnapshotStore:
    """Applies snapshots to the metadata store."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- data source -------------------------------------------------------

    async def ensure_data_source(self, snapshot: Snapshot, name: str | None = None) -> DataSource:
        key = name or snapshot.data_source
        existing = await self.session.scalar(
            select(DataSource).where(DataSource.name == key)
        )
        if existing is not None:
            existing.dialect = snapshot.dialect
            existing.database_name = snapshot.database
            existing.server_version = snapshot.server_version
            existing.updated_at = _now()
            return existing

        created = DataSource(
            name=key,
            dialect=snapshot.dialect,
            database_name=snapshot.database,
            server_version=snapshot.server_version,
        )
        self.session.add(created)
        await self.session.flush()
        return created

    async def latest_snapshot(self, data_source_id) -> SnapshotRecord | None:
        return await self.session.scalar(
            select(SnapshotRecord)
            .where(SnapshotRecord.data_source_id == data_source_id)
            .order_by(SnapshotRecord.captured_at.desc())
            .limit(1)
        )

    # -- apply -------------------------------------------------------------

    async def apply(
        self,
        snapshot: Snapshot,
        *,
        data_source_name: str | None = None,
        diff: DiffResult | None = None,
    ) -> ApplyResult:
        data_source = await self.ensure_data_source(snapshot, data_source_name)

        record = SnapshotRecord(
            data_source_id=data_source.id,
            structure_hash=snapshot.structure_hash,
            data_hash=snapshot.data_hash,
            captured_at=snapshot.captured_at,
            server_version=snapshot.server_version,
            table_count=sum(1 for t in snapshot.tables if t.kind == "table"),
            view_count=sum(1 for t in snapshot.tables if t.kind == "view"),
            column_count=snapshot.column_count,
            document=snapshot.to_dict(),
        )
        self.session.add(record)
        await self.session.flush()

        result = ApplyResult(
            snapshot_id=str(record.id),
            data_source_id=str(data_source.id),
            diff=diff,
        )

        existing_tables = {
            (t.schema_name, t.object_name): t
            for t in await self.session.scalars(
                select(TableRecord).where(TableRecord.data_source_id == data_source.id)
            )
        }

        seen: set[tuple[str, str]] = set()
        for table in snapshot.tables:
            key = (table.schema, table.name)
            seen.add(key)
            existing = existing_tables.get(key)
            written = await self._apply_table(
                table, existing, data_source.id, record.id, result
            )
            if written:
                result.tables_written += 1
            else:
                result.tables_unchanged += 1

        for key, existing in existing_tables.items():
            if key in seen:
                continue
            if not existing.is_tombstoned:
                existing.is_tombstoned = True
                existing.last_seen_at = _now()
                result.tables_tombstoned += 1
                result.notes.append(f"tombstoned {key[0]}.{key[1]}")

        await self.session.flush()
        return result



    # -- per-table upsert --------------------------------------------------

    async def _apply_table(
        self,
        table: TableSnapshot,
        existing: TableRecord | None,
        data_source_id,
        snapshot_id,
        result: ApplyResult,
    ) -> bool:
        """Write one table and its children. Returns True if anything changed."""
        new_structure = table.structure_hash
        new_data = table.data_hash
        new_columns_hash = _hash([c.structure_payload() for c in table.columns])
        now = _now()

        if existing is None:
            record = TableRecord(
                data_source_id=data_source_id,
                schema_name=table.schema,
                object_name=table.name,
                kind=table.kind,
                structure_hash=new_structure,
                column_structure_hash=new_columns_hash,
                data_hash=new_data,
                last_snapshot_id=snapshot_id,
                first_seen_at=now,
                last_seen_at=now,
                structure_changed_at=now,
            )
            self._update_data_fields(record, table)
            self.session.add(record)
            await self.session.flush()
            await self._apply_children(record, table, result, replace_all=True)
            return True

        structure_changed = existing.structure_hash != new_structure
        data_changed = existing.data_hash != new_data

        if not structure_changed and not data_changed:
            existing.last_seen_at = now
            existing.last_snapshot_id = snapshot_id
            existing.is_tombstoned = False
            return False

        # Data-shape fields are refreshed even when only data moved.
        self._update_data_fields(existing, table)
        existing.last_seen_at = now
        existing.last_snapshot_id = snapshot_id
        existing.is_tombstoned = False

        if structure_changed:
            existing.structure_changed_at = now
            existing.structure_hash = new_structure
            existing.column_structure_hash = new_columns_hash
            existing.kind = table.kind
            existing.comment = table.comment
        existing.data_hash = new_data

        if structure_changed:
            await self._apply_children(existing, table, result, replace_all=False)

        return True

    @staticmethod
    def _update_data_fields(record: TableRecord, table: TableSnapshot) -> None:
        record.estimated_rows = table.estimated_rows
        record.size_bytes = table.size_bytes
        record.modification_counter = table.modification_counter
        record.stats_updated_at = table.stats_updated_at
        record.partition_count = table.partition_count
        record.comment = table.comment


    async def _apply_children(
        self,
        record: TableRecord,
        table: TableSnapshot,
        result: ApplyResult,
        *,
        replace_all: bool,
    ) -> None:
        """Upsert columns, constraints, relations and indexes.

        Children are only touched when the table's structure changed. Columns
        missing from the new snapshot are tombstoned rather than deleted so that
        historical semantic versions keep a resolvable column reference.
        """
        now = _now()

        existing_columns = {
            c.column_name: c
            for c in await self.session.scalars(
                select(ColumnRecord).where(ColumnRecord.table_id == record.id)
            )
        }
        seen_columns: set[str] = set()
        for col in table.columns:
            seen_columns.add(col.name)
            col_hash = col.structure_hash
            current = existing_columns.get(col.name)
            if current is None:
                self.session.add(
                    ColumnRecord(
                        table_id=record.id,
                        column_name=col.name,
                        ordinal=col.ordinal,
                        data_type=col.data_type,
                        nullable=col.nullable,
                        default_expression=col.default,
                        comment=col.comment,
                        is_identity=col.is_identity,
                        is_computed=col.is_computed,
                        is_sparse=col.is_sparse,
                        is_rowguidcol=col.is_rowguidcol,
                        is_user_defined_type=col.is_user_defined_type,
                        max_length=col.max_length,
                        precision=col.precision,
                        scale=col.scale,
                        collation_name=col.collation,
                        computed_definition=col.computed_definition,
                        structure_hash=col_hash,
                        last_seen_at=now,
                        structure_changed_at=now,
                    )
                )
                result.columns_written += 1
                continue

            if current.structure_hash == col_hash and not current.is_tombstoned:
                current.last_seen_at = now
                current.ordinal = col.ordinal
                continue

            current.ordinal = col.ordinal
            current.data_type = col.data_type
            current.nullable = col.nullable
            current.default_expression = col.default
            current.comment = col.comment
            current.is_identity = col.is_identity
            current.is_computed = col.is_computed
            current.is_sparse = col.is_sparse
            current.is_rowguidcol = col.is_rowguidcol
            current.is_user_defined_type = col.is_user_defined_type
            current.max_length = col.max_length
            current.precision = col.precision
            current.scale = col.scale
            current.collation_name = col.collation
            current.computed_definition = col.computed_definition
            current.last_seen_at = now
            current.is_tombstoned = False
            if current.structure_hash != col_hash:
                current.structure_hash = col_hash
                current.structure_changed_at = now
            result.columns_written += 1

        for name, current in existing_columns.items():
            if name not in seen_columns and not current.is_tombstoned:
                current.is_tombstoned = True
                result.notes.append(f"tombstoned column {record.schema_name}."
                                    f"{record.object_name}.{name}")

        await self._replace_constraints(record, table)
        await self._replace_relations(record, table, result)
        await self._replace_indexes(record, table)


    async def _replace_constraints(self, record: TableRecord, table: TableSnapshot) -> None:
        """Constraints carry no inbound references, so replace wholesale.

        Deleted-and-reinserted only when the table's structure hash changed, so
        this never runs on a no-op capture.
        """
        await self.session.execute(
            sa_delete(ConstraintRecord).where(ConstraintRecord.table_id == record.id)
        )
        for c in table.constraints:
            self.session.add(
                ConstraintRecord(
                    table_id=record.id,
                    constraint_name=c.name,
                    kind=c.kind,
                    column_names=c.columns,
                    definition=c.definition,
                    is_disabled=c.is_disabled,
                    is_not_trusted=c.is_not_trusted,
                    structure_hash=_hash(c.structure_payload()),
                )
            )

    async def _replace_relations(
        self, record: TableRecord, table: TableSnapshot, result: ApplyResult
    ) -> None:
        await self.session.execute(
            sa_delete(RelationRecord).where(RelationRecord.table_id == record.id)
        )
        for r in table.relations:
            self.session.add(
                RelationRecord(
                    table_id=record.id,
                    relation_name=r.name,
                    from_schema=r.from_schema,
                    from_table=r.from_table,
                    from_columns=r.from_columns,
                    to_schema=r.to_schema,
                    to_table=r.to_table,
                    to_columns=r.to_columns,
                    on_delete=r.on_delete,
                    on_update=r.on_update,
                    is_disabled=r.is_disabled,
                    is_not_trusted=r.is_not_trusted,
                    structure_hash=_hash(r.structure_payload()),
                )
            )
            result.relations_written += 1

    async def _replace_indexes(self, record: TableRecord, table: TableSnapshot) -> None:
        await self.session.execute(
            sa_delete(IndexRecord).where(IndexRecord.table_id == record.id)
        )
        for i in table.indexes:
            self.session.add(
                IndexRecord(
                    table_id=record.id,
                    index_name=i.name,
                    is_primary_key=i.is_primary_key,
                    is_unique=i.is_unique,
                    is_unique_constraint=i.is_unique_constraint,
                    type_desc=i.type_desc,
                    filter_definition=i.filter_definition,
                    is_disabled=i.is_disabled,
                    columns=i.columns,
                    structure_hash=_hash(i.structure_payload()),
                )
            )

