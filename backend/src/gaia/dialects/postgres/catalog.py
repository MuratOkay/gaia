"""PostgreSQL catalog reader — the counterpart of ``dialects/mssql/catalog.py``.

Produces the same canonical ``Snapshot`` as the SQL Server reader, from a
fixed number of set-based ``pg_catalog`` queries in one ``REPEATABLE READ``
read-only transaction.

Mapping decisions, each chosen so downstream code (hashing, bundles, scoring)
behaves the same for both dialects:

* **Objects.** Tables (``r``) and partitioned tables (``p``) are ``table``;
  views (``v``) and materialized views (``m``) are ``view``. Partitions are
  folded into their parent (``partition_count``); they are storage, not
  meaning. Foreign tables are skipped: reading them would query a remote
  server. System schemas and extension-owned objects are excluded.
* **Types** are rendered the way a PostgreSQL user writes them
  (``varchar(20)``, ``timestamptz``, ``numeric(14,2)``, ``integer[]``). User types
  are schema-qualified because the session pins ``search_path = pg_catalog``.
  Domains are rendered as their **base type**, with the domain name and its
  checks in ``type_definition``; enums keep their type name, with the labels in
  ``type_definition``.
* **Identity.** ``GENERATED … AS IDENTITY`` and ``serial`` columns (a default of
  ``nextval`` on an owned sequence) are both ``is_identity`` — the same meaning
  as SQL Server's identity. The ``nextval`` default is kept for serials.
* **Computed.** Stored generated columns are ``is_computed`` with their
  expression in ``computed_definition``.
* **Trust.** ``NOT VALID`` constraints are ``is_not_trusted``. PostgreSQL cannot
  disable a constraint, but a foreign key whose enforcement triggers are
  disabled (``ALTER TABLE … DISABLE TRIGGER ALL``) is reported ``is_disabled`` —
  the same guarantee lost as a disabled SQL Server FK.
* **Check definitions** are the expression only (``CHECK`` keyword and
  ``NOT VALID`` suffix removed), and their columns come from ``conkey``, so
  nothing has to be recovered by parsing.
* **Indexes.** ``type_desc`` is the access method (``BTREE``, ``GIN`` …).
  Expression keys appear under their expression text. Invalid indexes (a failed
  ``CREATE INDEX CONCURRENTLY``) are ``is_disabled``.
* **Rows and freshness.** ``estimated_rows`` is ``reltuples`` (``-1`` = never
  analyzed → ``n_live_tup``); ``size_bytes`` is ``pg_table_size`` (heap + TOAST,
  no indexes, like SQL Server's clustered/heap pages); ``modification_counter``
  is ``n_mod_since_analyze`` and ``stats_updated_at`` the latest (auto)analyze.
  Partitioned tables sum their leaves.
* **NOT NULL** constraints, which PostgreSQL 18 also lists in
  ``pg_constraint``, are read from ``attnotnull`` only, like every other version.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from gaia.core.config import TargetConfig
from gaia.dialects.postgres.connection import GuardedCursor, session
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

__all__ = ["PostgresCatalogReader", "decode_typmod", "normalize_type"]

_SYSTEM_SCHEMA_FILTER = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%'
    AND n.nspname NOT LIKE 'pg\\_temp\\_%'
"""

_REFERENTIAL_ACTIONS = {
    "a": "NO_ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET_NULL",
    "d": "SET_DEFAULT",
}

_ALIASES = {
    "character varying": "varchar",
    "character": "char",
    "bpchar": "char",
    "bit varying": "varbit",
}
_TIME_TZ = re.compile(r"^(timestamp|time)(\(\d+\))? with(out)? time zone$")
_WITH_ARGS = re.compile(r"^(character varying|character|bit varying|bpchar)(\(.*\))?$")


def normalize_type(formatted: str) -> str:
    """Shorten ``format_type`` output to the names PostgreSQL users write.

    ``character varying(20)`` → ``varchar(20)``, ``timestamp(3) with time zone``
    → ``timestamptz(3)``, ``character(1)[]`` → ``char(1)[]``. Everything else,
    including user-defined and schema-qualified names, is returned unchanged.
    """
    suffix = ""
    core = formatted.strip()
    while core.endswith("[]"):
        suffix += "[]"
        core = core[:-2].rstrip()
    m = _TIME_TZ.match(core)
    if m:
        base = m.group(1) + ("" if m.group(3) else "tz")
        return f"{base}{m.group(2) or ''}{suffix}"
    m = _WITH_ARGS.match(core)
    if m:
        return f"{_ALIASES[m.group(1)]}{m.group(2) or ''}{suffix}"
    return core + suffix


def decode_typmod(typname: str, typmod: int) -> tuple[int | None, int | None, int | None]:
    """``(max_length, precision, scale)`` from a type modifier.

    Mirrors what SQL Server exposes in ``sys.columns``: a character length for
    ``varchar``/``char``, precision and scale for ``numeric``, and fractional
    seconds precision for time types. ``None`` where not applicable.
    """
    if typmod is None or typmod < 0:
        return None, None, None
    if typname in ("varchar", "bpchar"):
        return (typmod - 4 if typmod > 4 else None), None, None
    if typname == "numeric":
        value = typmod - 4
        precision = (value >> 16) & 0xFFFF
        scale = value & 0xFFFF
        if scale & 0x400:  # negative scale (PostgreSQL 15+), 11-bit two's complement
            scale -= 0x800
        return None, precision, scale
    if typname in ("timestamp", "timestamptz", "time", "timetz"):
        return None, typmod, None
    return None, None, None


def _strip_check(definition: str | None) -> str | None:
    """``CHECK ((x > 0)) NOT VALID`` → ``((x > 0))``."""
    if definition is None:
        return None
    text = definition.strip()
    if text.upper().endswith(" NOT VALID"):
        text = text[: -len(" NOT VALID")].rstrip()
    if text.upper().startswith("CHECK "):
        text = text[len("CHECK ") :].strip()
    return text


class PostgresCatalogReader:
    """Implements ``gaia.dialects.base.MetadataReader`` for PostgreSQL."""

    dialect = "postgres"

    def __init__(self, config: TargetConfig) -> None:
        self.config = config

    # -- public API --------------------------------------------------------------

    def read_snapshot(self, *, data_source: str | None = None) -> Snapshot:
        with session(self.config) as cur:
            server_version = str(self._scalar(cur, "SELECT current_setting('server_version')"))
            database = str(self._scalar(cur, "SELECT current_database()"))

            objects = self._read_objects(cur)
            oids = [o[0] for o in objects]
            columns = self._read_columns(cur, oids)
            constraints = self._read_constraints(cur, oids)
            relations = self._read_foreign_keys(cur, oids)
            indexes = self._read_indexes(cur, oids)
            sizes = self._read_sizes(cur, [o[0] for o in objects if o[4] in ("r", "p", "m")])

        tables: list[TableSnapshot] = []
        for oid, schema_name, name, kind, _relkind, comment in objects:
            size = sizes.get(oid, {})
            table = TableSnapshot(
                schema=schema_name,
                name=name,
                kind=kind,
                comment=comment,
                estimated_rows=size.get("rows"),
                size_bytes=size.get("size_bytes"),
                modification_counter=size.get("modification_counter"),
                stats_updated_at=size.get("stats_updated_at"),
                partition_count=size.get("partition_count", 1),
            )
            table.columns.extend(columns.get(oid, []))
            table.constraints.extend(constraints.get(oid, []))
            table.relations.extend(relations.get(oid, []))
            table.indexes.extend(indexes.get(oid, []))
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

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _scalar(cur: GuardedCursor, query: str) -> Any:
        cur.execute(query)
        row = cur.fetchone()
        return None if row is None else row[0]

    # -- catalog queries ---------------------------------------------------------

    def _read_objects(
        self, cur: GuardedCursor
    ) -> list[tuple[int, str, str, str, str, str | None]]:
        cur.execute(
            f"""
            SELECT c.oid, n.nspname, c.relname,
                   CASE WHEN c.relkind IN ('v', 'm') THEN 'view' ELSE 'table' END,
                   c.relkind::text,
                   obj_description(c.oid, 'pg_class')
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm')
              AND NOT c.relispartition
              AND {_SYSTEM_SCHEMA_FILTER}
              AND NOT EXISTS (
                    SELECT 1 FROM pg_depend d
                    WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid
                      AND d.deptype = 'e')
            ORDER BY n.nspname, c.relname
            """  # noqa: S608 (module constants only)
        )
        return [(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in cur.fetchall()]

    def _read_columns(
        self, cur: GuardedCursor, oids: list[int]
    ) -> dict[int, list[ColumnSnapshot]]:
        cur.execute(
            """
            SELECT a.attrelid,
                   a.attnum,
                   a.attname,
                   format_type(a.atttypid, a.atttypmod),
                   t.typname,
                   a.atttypmod,
                   NOT a.attnotnull,
                   a.attidentity::text,
                   a.attgenerated::text,
                   pg_get_expr(ad.adbin, ad.adrelid),
                   co.collname,
                   t.typtype::text,
                   format_type(t.typbasetype, t.typtypmod),
                   bt.typname,
                   t.typtypmod,
                   format_type(a.atttypid, NULL),
                   tn.nspname,
                   col_description(a.attrelid, a.attnum),
                   EXISTS (
                       SELECT 1 FROM pg_depend d
                       JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
                       WHERE d.classid = 'pg_class'::regclass
                         AND d.refobjid = a.attrelid
                         AND d.refobjsubid = a.attnum
                         AND d.deptype IN ('a', 'i')),
                   CASE WHEN t.typtype = 'e' THEN ARRAY(
                       SELECT e.enumlabel::text FROM pg_enum e
                       WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder) END,
                   CASE WHEN t.typtype = 'd' THEN ARRAY(
                       SELECT pg_get_constraintdef(dc.oid, true) FROM pg_constraint dc
                       WHERE dc.contypid = t.oid AND dc.contype = 'c' ORDER BY dc.conname) END
            FROM pg_attribute a
            JOIN pg_type t ON t.oid = a.atttypid
            JOIN pg_namespace tn ON tn.oid = t.typnamespace
            LEFT JOIN pg_type bt ON bt.oid = t.typbasetype
            LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            LEFT JOIN pg_collation co ON co.oid = a.attcollation AND a.attcollation <> 0
            WHERE a.attrelid = ANY(%s)
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attrelid, a.attnum
            """,
            (oids,),
        )
        out: dict[int, list[ColumnSnapshot]] = {}
        for row in cur.fetchall():
            (relid, attnum, name, formatted, typname, typmod, nullable, identity, generated,
             expr, collation, typtype, domain_base, domain_typname, domain_typmod,
             type_name_only, type_schema, comment, owns_sequence, enum_labels,
             domain_checks) = row

            is_domain = typtype == "d"
            type_definition: str | None = None
            if is_domain:
                # Render the base type; keep the domain as its definition.
                data_type = normalize_type(domain_base)
                max_length, precision, scale = decode_typmod(domain_typname, domain_typmod)
                checks = " ".join(domain_checks or [])
                type_definition = f"domain {type_name_only}" + (f": {checks}" if checks else "")
            else:
                data_type = normalize_type(formatted)
                max_length, precision, scale = decode_typmod(typname, typmod)
                if typtype == "e":
                    labels = ", ".join("'" + lbl.replace("'", "''") + "'" for lbl in enum_labels)
                    type_definition = f"enum ({labels})"

            is_generated = bool(generated)
            is_identity = identity in ("a", "d") or bool(
                owns_sequence and expr and expr.startswith("nextval(")
            )
            out.setdefault(int(relid), []).append(
                ColumnSnapshot(
                    name=name,
                    ordinal=int(attnum),
                    data_type=data_type,
                    nullable=bool(nullable),
                    default=None if is_generated else expr,
                    comment=comment,
                    is_identity=is_identity,
                    is_computed=is_generated,
                    computed_definition=expr if is_generated else None,
                    collation=collation,
                    is_user_defined_type=(
                        typtype in ("e", "d", "c", "r", "m")
                        or type_schema not in ("pg_catalog", "information_schema")
                    ),
                    max_length=max_length,
                    precision=precision,
                    scale=scale,
                    type_definition=type_definition,
                )
            )
        return out

    def _read_constraints(
        self, cur: GuardedCursor, oids: list[int]
    ) -> dict[int, list[ConstraintSnapshot]]:
        """Primary key, unique, check and exclusion constraints (FKs separately)."""
        cur.execute(
            """
            SELECT con.conrelid,
                   con.conname,
                   con.contype::text,
                   con.convalidated,
                   pg_get_constraintdef(con.oid, true),
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                       ORDER BY k.ord)
            FROM pg_constraint con
            WHERE con.conrelid = ANY(%s)
              AND con.contype IN ('p', 'u', 'c', 'x')
            ORDER BY con.conrelid, con.conname
            """,
            (oids,),
        )
        kinds = {"p": "primary_key", "u": "unique", "c": "check", "x": "exclusion"}
        out: dict[int, list[ConstraintSnapshot]] = {}
        for relid, name, contype, validated, definition, cols in cur.fetchall():
            kind = kinds[contype]
            out.setdefault(int(relid), []).append(
                ConstraintSnapshot(
                    name=name,
                    kind=kind,
                    columns=list(cols or []),
                    definition=(
                        _strip_check(definition) if kind == "check"
                        else definition if kind == "exclusion" else None
                    ),
                    is_not_trusted=not validated,
                )
            )
        return out

    def _read_foreign_keys(
        self, cur: GuardedCursor, oids: list[int]
    ) -> dict[int, list[RelationSnapshot]]:
        cur.execute(
            """
            SELECT con.conrelid,
                   con.conname,
                   sn.nspname, sc.relname,
                   tn.nspname, tc.relname,
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                       ORDER BY k.ord),
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
                       ORDER BY k.ord),
                   con.confdeltype::text,
                   con.confupdtype::text,
                   con.convalidated,
                   EXISTS (SELECT 1 FROM pg_trigger tg
                           WHERE tg.tgconstraint = con.oid AND tg.tgenabled = 'D')
            FROM pg_constraint con
            JOIN pg_class sc ON sc.oid = con.conrelid
            JOIN pg_namespace sn ON sn.oid = sc.relnamespace
            JOIN pg_class tc ON tc.oid = con.confrelid
            JOIN pg_namespace tn ON tn.oid = tc.relnamespace
            WHERE con.contype = 'f'
              AND con.conrelid = ANY(%s)
              AND con.conparentid = 0
            ORDER BY con.conrelid, con.conname
            """,
            (oids,),
        )
        out: dict[int, list[RelationSnapshot]] = {}
        for (relid, name, from_schema, from_table, to_schema, to_table, from_cols, to_cols,
             on_delete, on_update, validated, triggers_disabled) in cur.fetchall():
            out.setdefault(int(relid), []).append(
                RelationSnapshot(
                    name=name,
                    from_schema=from_schema,
                    from_table=from_table,
                    from_columns=list(from_cols),
                    to_schema=to_schema,
                    to_table=to_table,
                    to_columns=list(to_cols),
                    on_delete=_REFERENTIAL_ACTIONS.get(on_delete),
                    on_update=_REFERENTIAL_ACTIONS.get(on_update),
                    is_disabled=bool(triggers_disabled),
                    is_not_trusted=not validated,
                )
            )
        return out

    def _read_indexes(self, cur: GuardedCursor, oids: list[int]) -> dict[int, list[IndexSnapshot]]:
        cur.execute(
            """
            SELECT i.indrelid,
                   ic.relname,
                   i.indisprimary,
                   i.indisunique,
                   EXISTS (SELECT 1 FROM pg_constraint c
                           WHERE c.conindid = i.indexrelid AND c.contype = 'u'),
                   upper(am.amname),
                   pg_get_expr(i.indpred, i.indrelid),
                   NOT (i.indisvalid AND i.indisready),
                   k.n,
                   a.attname,
                   pg_get_indexdef(i.indexrelid, k.n, true),
                   (i.indoption[k.n - 1] & 1) = 1,
                   k.n > i.indnkeyatts
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_am am ON am.oid = ic.relam
            CROSS JOIN LATERAL generate_series(1, i.indnatts) AS k(n)
            LEFT JOIN pg_attribute a
                   ON a.attrelid = i.indrelid AND a.attnum = i.indkey[k.n - 1]
                  AND i.indkey[k.n - 1] <> 0
            WHERE i.indrelid = ANY(%s)
            ORDER BY i.indrelid, ic.relname, k.n
            """,
            (oids,),
        )
        acc: dict[tuple[int, str], IndexSnapshot] = {}
        for (relid, name, is_pk, is_unique, is_uc, amname, predicate, disabled, position,
             attname, keydef, descending, included) in cur.fetchall():
            snap = acc.get((int(relid), name))
            if snap is None:
                snap = IndexSnapshot(
                    name=name,
                    is_primary_key=bool(is_pk),
                    is_unique=bool(is_unique),
                    is_unique_constraint=bool(is_uc),
                    type_desc=amname,
                    filter_definition=predicate,
                    is_disabled=bool(disabled),
                )
                acc[(int(relid), name)] = snap
            snap.columns.append(
                {
                    "name": attname if attname is not None else keydef,
                    "key_ordinal": 0 if included else int(position),
                    "descending": bool(descending) and not included,
                    "included": bool(included),
                }
            )
        out: dict[int, list[IndexSnapshot]] = {}
        for (relid, _name), snap in acc.items():
            out.setdefault(relid, []).append(snap)
        return out

    def _read_sizes(self, cur: GuardedCursor, oids: list[int]) -> dict[int, dict[str, Any]]:
        """Rows, size and statistics freshness, summed over partitions."""
        if not oids:
            return {}
        cur.execute(
            """
            SELECT o.oid,
                   count(*),
                   sum(CASE WHEN lc.reltuples >= 0 THEN lc.reltuples END),
                   sum(pg_stat_get_live_tuples(lc.oid)),
                   bool_and(lc.reltuples < 0),
                   sum(pg_table_size(lc.oid)),
                   sum(pg_stat_get_mod_since_analyze(lc.oid)),
                   max(GREATEST(pg_stat_get_last_analyze_time(lc.oid),
                                pg_stat_get_last_autoanalyze_time(lc.oid)))
            FROM unnest(%s::oid[]) AS o(oid)
            CROSS JOIN LATERAL (
                SELECT pt.relid FROM pg_partition_tree(o.oid) pt WHERE pt.isleaf
                UNION ALL
                SELECT o.oid WHERE NOT EXISTS (
                    SELECT 1 FROM pg_partition_tree(o.oid) pt WHERE pt.isleaf)
            ) AS leaf(relid)
            JOIN pg_class lc ON lc.oid = leaf.relid
            GROUP BY o.oid
            """,
            (oids,),
        )
        out: dict[int, dict[str, Any]] = {}
        for (oid, leaves, reltuples, live, never_analyzed, size_bytes, modified,
             analyzed_at) in cur.fetchall():
            rows = int(live) if never_analyzed and live is not None else (
                int(reltuples) if reltuples is not None else None
            )
            updated: datetime | None = analyzed_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            out[int(oid)] = {
                "rows": rows,
                "size_bytes": int(size_bytes) if size_bytes is not None else None,
                "partition_count": int(leaves),
                "modification_counter": int(modified) if modified is not None else None,
                "stats_updated_at": updated,
            }
        return out
