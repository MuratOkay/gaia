"""SQL Server catalog reader.

Reads the whole schema in a fixed number of set-based queries rather than one
query per table. At 607 columns and 68 tables the difference is seconds vs
minutes, and it also means the snapshot is captured against a consistent
picture within a single transaction.

Deliberate choices, each verified against SQL Server 2025:

* **`sys.objects.modify_date` is ignored entirely.** Adding a comment via
  ``sp_addextendedproperty`` does not bump it, so it cannot signal change.
  Only content hashes can.
* **Comments come from ``sys.extended_properties``** where
  ``name = 'MS_Description'``, at ``minor_id = 0`` for tables and
  ``minor_id = column_id`` for columns.
* **Row counts prefer ``sys.dm_db_partition_stats``** (needs ``VIEW DATABASE
  STATE``) and fall back to ``sys.partitions.rows``, recording which was used.
* **Only ``is_ms_shipped = 0``** objects are read, so system tables never leak
  into the semantic model.
* Every session is hardened: read-uncommitted, lock timeout, and a statement
  timeout (applied since Phase 3.0; before that it was configured but not
  set), because this runs against production replicas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pyodbc

from gaia.core.config import TargetConfig
from gaia.dialects.mssql.connection import GuardedCursor, session
from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    IndexSnapshot,
    RelationSnapshot,
    Snapshot,
    TableSnapshot,
    finalize,
)

logger = logging.getLogger(__name__)

__all__ = ["MssqlCatalogReader", "format_type"]


# Types whose length is expressed in characters rather than bytes.
_NCHAR_TYPES = {"nchar", "nvarchar", "ntext"}
# Types with no meaningful user-facing length.
_UNSIZED_TYPES = {
    "int",
    "bigint",
    "smallint",
    "tinyint",
    "bit",
    "date",
    "datetime",
    "smalldatetime",
    "datetime2",
    "datetimeoffset",
    "time",
    "money",
    "smallmoney",
    "uniqueidentifier",
    "rowversion",
    "timestamp",
    "image",
    "xml",
    "sql_variant",
    "hierarchyid",
    "geography",
    "geometry",
    "text",
    "ntext",
    "sysname",
}


def format_type(
    type_name: str, max_length: int, precision: int, scale: int
) -> str:
    """Render a SQL Server type the way a DBA would write it.

    ``max_length = -1`` means MAX. For ``n*`` types ``max_length`` is bytes, so
    characters are half that.
    """
    lowered = type_name.lower()

    if lowered in ("decimal", "numeric"):
        return f"{lowered}({precision},{scale})"
    if lowered in ("datetime2", "datetimeoffset", "time"):
        return f"{lowered}({scale})"
    if lowered in ("float",):
        return f"float({precision})"

    if lowered in _UNSIZED_TYPES:
        return lowered

    if max_length == -1:
        if lowered in _NCHAR_TYPES:
            return f"{lowered}(max)"
        return f"{lowered}(max)"

    if lowered in _NCHAR_TYPES:
        return f"{lowered}({max_length // 2})"

    return f"{lowered}({max_length})"


class MssqlCatalogReader:
    """Implements ``gaia.dialects.base.MetadataReader`` for SQL Server."""

    dialect = "mssql"

    def __init__(self, config: TargetConfig) -> None:
        self.config = config

    # -- connection --------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[GuardedCursor]:
        """A hardened read-only session with a **single** cursor.

        See ``gaia.dialects.mssql.connection``: isolation, lock timeout and —
        since Phase 3.0 — the statement timeout are applied there. One cursor
        only: creating a second while the first is unreferenced makes pyodbc
        lose the active statement handle, and ``fetchone()`` then silently
        returns ``None`` (a Phase 1 defect).
        """
        with session(self.config) as cur:
            yield cur

    # -- public API --------------------------------------------------------

    def read_snapshot(self, *, data_source: str | None = None) -> Snapshot:
        with self._session() as cur:
            server_version = str(
                self._scalar(cur, "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50))")
            )
            database = str(self._scalar(cur, "SELECT DB_NAME()"))

            objects = self._read_objects(cur)
            columns = self._read_columns(cur)
            defaults = self._read_column_defaults(cur)
            table_comments = self._read_comments(cur, minor_id_zero=True)
            column_comments = self._read_comments(cur, minor_id_zero=False)
            constraints = self._read_key_and_check_constraints(cur)
            relations = self._read_foreign_keys(cur)
            indexes = self._read_indexes(cur)
            partitions = self._read_partitions(cur)
            stats = self._read_stats(cur)


        tables: list[TableSnapshot] = []
        for (object_id, schema_name, table_name, kind) in objects:
            table = TableSnapshot(
                schema=schema_name,
                name=table_name,
                kind=kind,
                comment=table_comments.get(object_id),
                partition_count=partitions.get(object_id, {}).get("partition_count", 1),
            )

            rows = partitions.get(object_id, {})
            table.estimated_rows = rows.get("row_count")
            table.size_bytes = rows.get("size_bytes")

            stat = stats.get(object_id)
            if stat:
                table.modification_counter = stat.get("modification_counter")
                table.stats_updated_at = stat.get("stats_updated_at")

            for (col_object_id, col) in columns.get(object_id, []):
                if col.comment is None:
                    col.comment = column_comments.get((object_id, col_object_id))
                default = defaults.get((object_id, col_object_id))
                if default is not None:
                    col.default = default
                table.columns.append(col)

            for c in constraints.get(object_id, []):
                table.constraints.append(c)
            for r in relations.get(object_id, []):
                table.relations.append(r)
            for i in indexes.get(object_id, []):
                table.indexes.append(i)

            tables.append(table)

        return finalize(
            Snapshot(
                data_source=data_source or self.config.database,
                dialect=self.dialect,
                database=database,
                server_version=server_version,
                tables=tables,
            )
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _scalar(cur: GuardedCursor, sql: str) -> Any:
        cur.execute(sql)
        row = cur.fetchone()
        return None if row is None else row[0]

    # -- catalog queries ---------------------------------------------------

    def _read_objects(self, cur: GuardedCursor) -> list[tuple[int, str, str, str]]:
        """Tables and views, excluding system objects."""
        cur.execute(
            """
            SELECT o.object_id,
                   s.name AS schema_name,
                   o.name AS object_name,
                   CASE WHEN o.type = 'V' THEN 'view' ELSE 'table' END AS kind
            FROM sys.objects o
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.is_ms_shipped = 0
              AND o.type IN ('U', 'V')
            ORDER BY s.name, o.name
            """
        )
        return [
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in cur.fetchall()
        ]

    def _read_columns(
        self, cur: GuardedCursor
    ) -> dict[int, list[tuple[int, ColumnSnapshot]]]:
        """All columns keyed by owning object_id.

        Physical characteristics (identity, sparse, rowguid, computed) and the
        numeric type parameters are captured here; comments and defaults are
        merged in by the caller.
        """
        cur.execute(
            """
            SELECT c.object_id,
                   c.column_id,
                   c.name,
                   TYPE_NAME(c.user_type_id) AS type_name,
                   c.max_length,
                   c.precision,
                   c.scale,
                   c.is_nullable,
                   c.is_identity,
                   c.is_computed,
                   c.is_sparse,
                   c.is_rowguidcol,
                   c.collation_name,
                   CAST(t.is_user_defined AS bit) AS is_user_defined,
                   cc.definition AS computed_definition
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.types t ON t.user_type_id = c.user_type_id
            LEFT JOIN sys.computed_columns cc
                   ON cc.object_id = c.object_id AND cc.column_id = c.column_id
            WHERE o.is_ms_shipped = 0
              AND o.type IN ('U', 'V')
            ORDER BY c.object_id, c.column_id
            """
        )

        result: dict[int, list[tuple[int, ColumnSnapshot]]] = {}
        for row in cur.fetchall():
            object_id = int(row[0])
            column_id = int(row[1])
            type_name = str(row[3])
            max_length = int(row[4])
            precision = int(row[5])
            scale = int(row[6])

            col = ColumnSnapshot(
                name=str(row[2]),
                ordinal=column_id,
                data_type=format_type(type_name, max_length, precision, scale),
                nullable=bool(row[7]),
                is_identity=bool(row[8]),
                is_computed=bool(row[9]),
                is_sparse=bool(row[10]),
                is_rowguidcol=bool(row[11]),
                collation=str(row[12]) if row[12] is not None else None,
                is_user_defined_type=bool(row[13]),
                computed_definition=str(row[14]) if row[14] is not None else None,
                max_length=None if max_length in (-1, 0) else max_length,
                precision=precision if precision else None,
                scale=scale if scale else None,
            )
            result.setdefault(object_id, []).append((column_id, col))
        return result

    def _read_comments(
        self, cur: GuardedCursor, *, minor_id_zero: bool
    ) -> dict[Any, str]:
        """``MS_Description`` extended properties.

        ``minor_id = 0`` addresses the object itself (table/view comment);
        ``minor_id > 0`` addresses a column. Returns ``{object_id: text}`` for
        objects and ``{(object_id, column_id): text}`` for columns.
        """
        predicate = "ep.minor_id = 0" if minor_id_zero else "ep.minor_id > 0"
        cur.execute(
            f"""
            SELECT ep.major_id, ep.minor_id, CAST(ep.value AS nvarchar(4000))
            FROM sys.extended_properties ep
            JOIN sys.objects o ON o.object_id = ep.major_id
            WHERE ep.name = 'MS_Description'
              AND ep.class = 1
              AND {predicate}
              AND o.is_ms_shipped = 0
            """
        )
        out: dict[Any, str] = {}
        for major_id, minor_id, value in cur.fetchall():
            if value is None:
                continue
            text = str(value)
            if minor_id_zero:
                out[int(major_id)] = text
            else:
                out[(int(major_id), int(minor_id))] = text
        return out

    def _read_column_defaults(self, cur: GuardedCursor) -> dict[tuple[int, int], str]:
        cur.execute(
            """
            SELECT dc.parent_object_id, dc.parent_column_id, dc.definition
            FROM sys.default_constraints dc
            JOIN sys.objects o ON o.object_id = dc.parent_object_id
            WHERE o.is_ms_shipped = 0
            """
        )
        return {
            (int(r[0]), int(r[1])): str(r[2])
            for r in cur.fetchall()
            if r[2] is not None
        }


    def _read_key_and_check_constraints(
        self, cur: GuardedCursor
    ) -> dict[int, list[ConstraintSnapshot]]:
        """PK, unique and check constraints, with ordered column lists."""
        out: dict[int, list[ConstraintSnapshot]] = {}

        cur.execute(
            """
            SELECT kc.parent_object_id,
                   kc.name,
                   CASE kc.type WHEN 'PK' THEN 'primary_key' ELSE 'unique' END AS kind,
                   c.name AS column_name,
                   ic.key_ordinal
            FROM sys.key_constraints kc
            JOIN sys.index_columns ic
                  ON ic.object_id = kc.parent_object_id
                 AND ic.index_id = kc.unique_index_id
            JOIN sys.columns c
                  ON c.object_id = ic.object_id
                 AND c.column_id = ic.column_id
            JOIN sys.objects o ON o.object_id = kc.parent_object_id
            WHERE o.is_ms_shipped = 0
            ORDER BY kc.parent_object_id, kc.name, ic.key_ordinal
            """
        )
        grouped: dict[tuple[int, str, str], list[str]] = {}
        for parent_id, name, kind, column_name, _ordinal in cur.fetchall():
            key = (int(parent_id), str(name), str(kind))
            grouped.setdefault(key, []).append(str(column_name))

        for (parent_id, name, kind), cols in grouped.items():
            out.setdefault(parent_id, []).append(
                ConstraintSnapshot(name=name, kind=kind, columns=cols)
            )

        cur.execute(
            """
            SELECT cc.parent_object_id,
                   cc.name,
                   cc.definition,
                   cc.is_disabled,
                   cc.is_not_trusted
            FROM sys.check_constraints cc
            JOIN sys.objects o ON o.object_id = cc.parent_object_id
            WHERE o.is_ms_shipped = 0
            ORDER BY cc.parent_object_id, cc.name
            """
        )
        for parent_id, name, definition, is_disabled, is_not_trusted in cur.fetchall():
            out.setdefault(int(parent_id), []).append(
                ConstraintSnapshot(
                    name=str(name),
                    kind="check",
                    definition=str(definition) if definition is not None else None,
                    is_disabled=bool(is_disabled),
                    is_not_trusted=bool(is_not_trusted),
                )
            )
        return out

    def _read_foreign_keys(self, cur: GuardedCursor) -> dict[int, list[RelationSnapshot]]:
        """Foreign keys, grouped per parent object with column order preserved."""
        cur.execute(
            """
            SELECT fk.parent_object_id,
                   fk.name,
                   ps.name AS from_schema,
                   po.name AS from_table,
                   rs.name AS to_schema,
                   ro.name AS to_table,
                   pc.name AS from_column,
                   rc.name AS to_column,
                   fkc.constraint_column_id,
                   fk.delete_referential_action_desc,
                   fk.update_referential_action_desc,
                   fk.is_disabled,
                   fk.is_not_trusted
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc
                  ON fkc.constraint_object_id = fk.object_id
            JOIN sys.objects po ON po.object_id = fk.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = po.schema_id
            JOIN sys.columns pc
                  ON pc.object_id = fkc.parent_object_id
                 AND pc.column_id = fkc.parent_column_id
            JOIN sys.objects ro ON ro.object_id = fk.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = ro.schema_id
            JOIN sys.columns rc
                  ON rc.object_id = fkc.referenced_object_id
                 AND rc.column_id = fkc.referenced_column_id
            WHERE po.is_ms_shipped = 0
            ORDER BY fk.parent_object_id, fk.name, fkc.constraint_column_id
            """
        )

        acc: dict[tuple[int, str], dict[str, Any]] = {}
        for row in cur.fetchall():
            parent_id = int(row[0])
            name = str(row[1])
            key = (parent_id, name)
            entry = acc.setdefault(
                key,
                {
                    "from_schema": str(row[2]),
                    "from_table": str(row[3]),
                    "to_schema": str(row[4]),
                    "to_table": str(row[5]),
                    "from_columns": [],
                    "to_columns": [],
                    "on_delete": str(row[9]) if row[9] else None,
                    "on_update": str(row[10]) if row[10] else None,
                    "is_disabled": bool(row[11]),
                    "is_not_trusted": bool(row[12]),
                },
            )
            entry["from_columns"].append(str(row[6]))
            entry["to_columns"].append(str(row[7]))

        out: dict[int, list[RelationSnapshot]] = {}
        for (parent_id, name), data in acc.items():
            out.setdefault(parent_id, []).append(
                RelationSnapshot(name=name, **data)
            )
        return out


    def _read_indexes(self, cur: GuardedCursor) -> dict[int, list[IndexSnapshot]]:
        cur.execute(
            """
            SELECT i.object_id, i.name, i.is_primary_key, i.is_unique,
                   i.is_unique_constraint, i.type_desc, i.filter_definition,
                   i.is_disabled
            FROM sys.indexes i
            JOIN sys.objects o ON o.object_id = i.object_id
            WHERE o.is_ms_shipped = 0
              AND o.type IN ('U', 'V')
              AND i.name IS NOT NULL
              AND i.type > 0
            ORDER BY i.object_id, i.name
            """
        )
        index_meta: dict[tuple[int, str], IndexSnapshot] = {}
        for row in cur.fetchall():
            index_meta[(int(row[0]), str(row[1]))] = IndexSnapshot(
                name=str(row[1]),
                is_primary_key=bool(row[2]),
                is_unique=bool(row[3]),
                is_unique_constraint=bool(row[4]),
                type_desc=str(row[5]),
                filter_definition=str(row[6]) if row[6] is not None else None,
                is_disabled=bool(row[7]),
            )

        cur.execute(
            """
            SELECT i.object_id, i.name, c.name AS column_name, ic.key_ordinal,
                   ic.is_descending_key, ic.is_included_column
            FROM sys.indexes i
            JOIN sys.objects o ON o.object_id = i.object_id
            JOIN sys.index_columns ic
                  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
                  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE o.is_ms_shipped = 0
              AND o.type IN ('U', 'V')
              AND i.name IS NOT NULL
              AND i.type > 0
            ORDER BY i.object_id, i.name, ic.is_included_column, ic.key_ordinal
            """
        )
        for object_id, index_name, column_name, key_ordinal, is_desc, is_included in (
            cur.fetchall()
        ):
            snap = index_meta.get((int(object_id), str(index_name)))
            if snap is None:
                continue
            snap.columns.append(
                {
                    "name": str(column_name),
                    "key_ordinal": int(key_ordinal),
                    "descending": bool(is_desc),
                    "included": bool(is_included),
                }
            )

        out: dict[int, list[IndexSnapshot]] = {}
        for (object_id, _name), snap in index_meta.items():
            out.setdefault(object_id, []).append(snap)
        return out


    def _read_partitions(self, cur: GuardedCursor) -> dict[int, dict[str, Any]]:
        """Row counts and size, preferring the DMV, falling back to sys.partitions."""
        out: dict[int, dict[str, Any]] = {}

        if self.config.capabilities.dmv_row_counts:
            try:
                cur.execute(
                    """
                    SELECT p.object_id,
                           SUM(CASE WHEN p.index_id IN (0, 1) THEN p.row_count ELSE 0 END),
                           SUM(CASE WHEN p.index_id IN (0, 1)
                                    THEN p.reserved_page_count ELSE 0 END) * 8192,
                           COUNT(DISTINCT p.partition_number)
                    FROM sys.dm_db_partition_stats p
                    JOIN sys.objects o ON o.object_id = p.object_id
                    WHERE o.is_ms_shipped = 0 AND o.type = 'U'
                    GROUP BY p.object_id
                    """
                )
                for object_id, row_count, size_bytes, partition_count in cur.fetchall():
                    out[int(object_id)] = {
                        "row_count": int(row_count or 0),
                        "size_bytes": int(size_bytes or 0),
                        "partition_count": int(partition_count or 1),
                        "method": "dmv",
                    }
                return out
            except pyodbc.Error as exc:
                logger.warning(
                    "dm_db_partition_stats unavailable (%s); falling back to "
                    "sys.partitions (approximate row counts)",
                    exc.args[0] if exc.args else exc,
                )

        cur.execute(
            """
            SELECT p.object_id, SUM(p.rows), COUNT(DISTINCT p.partition_number)
            FROM sys.partitions p
            JOIN sys.objects o ON o.object_id = p.object_id
            WHERE o.is_ms_shipped = 0 AND o.type = 'U' AND p.index_id IN (0, 1)
            GROUP BY p.object_id
            """
        )
        for object_id, row_count, partition_count in cur.fetchall():
            out[int(object_id)] = {
                "row_count": int(row_count or 0),
                "size_bytes": None,
                "partition_count": int(partition_count or 1),
                "method": "approx",
            }
        return out

    def _read_stats(self, cur: GuardedCursor) -> dict[int, dict[str, Any]]:
        """Statistics freshness, feeding the data hash.

        ``modification_counter`` counts changes since the last statistics
        update, which is the cheapest reliable signal that data moved without
        the structure changing.
        """
        out: dict[int, dict[str, Any]] = {}
        try:
            cur.execute(
                """
                SELECT s.object_id,
                       MAX(sp.modification_counter),
                       MAX(sp.last_updated)
                FROM sys.stats s
                JOIN sys.objects o ON o.object_id = s.object_id
                CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
                WHERE o.is_ms_shipped = 0 AND o.type = 'U'
                GROUP BY s.object_id
                """
            )
            for object_id, modification_counter, last_updated in cur.fetchall():
                updated: datetime | None = None
                if last_updated is not None:
                    updated = last_updated
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=UTC)
                out[int(object_id)] = {
                    "modification_counter": (
                        int(modification_counter) if modification_counter is not None else None
                    ),
                    "stats_updated_at": updated,
                }
        except pyodbc.Error as exc:
            logger.warning(
                "dm_db_stats_properties unavailable (%s); data hashes fall back to "
                "row counts only",
                exc.args[0] if exc.args else exc,
            )
        return out
