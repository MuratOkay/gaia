"""Table bundles: the catalog facts one reasoning call sees.

One bundle per table (PHASES, "Reasoning strategy"): table name, comment, row
estimate, columns with types/nullability/defaults, PK and unique constraints,
check constraints, indexes, and foreign keys plus the referenced table and
column names.

Two outputs per table:

* ``document``: what the model sees, rendered as deterministic JSON. Every
  constraint, index and foreign key carries a stable ``ref`` (for example
  ``fk:FK_orders_customers``) so the model can cite evidence precisely.
* ``facts``: per-column catalog facts the scorer uses. Scoring never takes
  the model's word for which evidence exists.

Everything here is a pure function of the ``TableSnapshot``, so a given
``structure_hash`` always renders the same bundle. The one exception is the
row estimate, which is a data signal and does not trigger re-reasoning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from gaia.introspect.snapshot import Snapshot, TableSnapshot

__all__ = [
    "GENERIC_REFS",
    "CatalogIndex",
    "ColumnFacts",
    "TableBundle",
    "build_bundle",
    "build_catalog_index",
]

# Evidence the model may cite that is not a named constraint or index. The
# fact-dependent ones are valid only when the fact exists for the column.
GENERIC_REFS: frozenset[str] = frozenset(
    {
        "name",
        "type",
        "nullability",
        "default",
        "comment",
        "identity",
        "computed",
        "table_name",
        "table_comment",
        "sibling_columns",
        "row_estimate",
    }
)

_BRACKETED = re.compile(r"\[([^\]]+)\]")


@dataclass
class ColumnFacts:
    """Catalog facts about one column, independent of any model output."""

    name: str
    data_type: str
    nullable: bool
    has_comment: bool = False
    has_default: bool = False
    is_identity: bool = False
    is_computed: bool = False
    is_rowguid: bool = False
    in_primary_key: bool = False
    primary_key_size: int = 0  # number of columns in the PK, 0 if not in it
    in_unique: bool = False  # unique constraint or non-filtered unique index
    foreign_keys: list[str] = field(default_factory=list)  # refs
    foreign_keys_trusted: bool = False  # at least one enabled, trusted FK
    checks: list[str] = field(default_factory=list)  # refs
    checks_trusted: bool = False  # at least one enabled, trusted check
    refs: set[str] = field(default_factory=set)  # every named ref involving it
    dialect: str = "mssql"  # vocabulary for type checks (gaia.dialects.types)
    type_definition: str | None = None  # enum/domain definition, PostgreSQL only

    def valid_generic_refs(self, table_has_comment: bool) -> set[str]:
        valid = {"name", "type", "nullability", "table_name", "sibling_columns", "row_estimate"}
        if self.has_comment:
            valid.add("comment")
        if self.has_default:
            valid.add("default")
        if self.is_identity:
            valid.add("identity")
        if self.is_computed:
            valid.add("computed")
        if table_has_comment:
            valid.add("table_comment")
        return valid


@dataclass
class TableBundle:
    schema: str
    name: str
    kind: str
    structure_hash: str
    document: dict[str, Any]
    facts: dict[str, ColumnFacts]  # column name -> facts, in ordinal order
    ref_columns: dict[str, set[str]]  # ref -> columns it involves
    has_comment: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def column_names(self) -> list[str]:
        return list(self.facts)

    def render(self) -> str:
        """Deterministic JSON (insertion order is fixed by ``build_bundle``)."""
        return json.dumps(self.document, indent=2, ensure_ascii=False)

    def ref_is_valid_for(self, ref: str, column: str) -> bool:
        """True when ``ref`` exists in this bundle and concerns ``column``."""
        facts = self.facts.get(column)
        if facts is None:
            return False
        if ref in GENERIC_REFS:
            return ref in facts.valid_generic_refs(self.has_comment)
        involved = self.ref_columns.get(ref)
        if involved is None:
            return False
        # A check constraint whose columns could not be resolved is
        # table-level and may be cited by any column.
        return not involved or column in involved


@dataclass
class CatalogIndex:
    """Names and primary keys of every object in the data source.

    Sent once per run in the stable prompt prefix so the model can suggest
    joins for reference-like columns that have no declared foreign key.
    """

    database: str
    entries: list[str]
    columns: dict[str, set[str]]  # "schema.table" -> column names

    def render(self) -> str:
        lines = "\n".join(self.entries)
        return f"Database: {self.database}\nObjects ({len(self.entries)}):\n{lines}"

    def has_table(self, qualified: str) -> bool:
        return qualified in self.columns


def _unique_keys(table: TableSnapshot) -> list[list[str]]:
    """Alternate keys: unique constraints and unfiltered, enabled unique indexes
    that are not the primary key, in a stable order."""
    pk = next((c.columns for c in table.constraints if c.kind == "primary_key"), None)
    keys: list[list[str]] = []
    for con in table.constraints:
        if con.kind == "unique" and con.columns:
            keys.append(list(con.columns))
    for idx in table.indexes:
        cols = [c["name"] for c in idx.columns if not c.get("included")]
        if idx.is_unique and not idx.filter_definition and not idx.is_disabled and cols:
            keys.append(cols)
    out: list[list[str]] = []
    for key in sorted(keys, key=lambda k: (len(k), k)):
        if key != (list(pk) if pk else None) and key not in out:
            out.append(key)
    return out


def build_catalog_index(snapshot: Snapshot, *, unique_keys: bool = False) -> CatalogIndex:
    """The object list. ``unique_keys`` (prompt generation 2) also lists
    alternate keys: with primary keys alone the model aimed code columns at
    surrogate ids (owner run 2026-11-15: ``f2 -> customers.customer_id`` and
    ``store_code -> stores.store_id``, where ``customer_code`` and
    ``store_code`` are the unique keys the values live in)."""
    entries: list[str] = []
    columns: dict[str, set[str]] = {}
    for table in sorted(snapshot.tables, key=lambda t: (t.schema, t.name)):
        pk = next((c for c in table.constraints if c.kind == "primary_key"), None)
        parts = [f"pk: {', '.join(pk.columns)}"] if pk and pk.columns else []
        if unique_keys:
            alternates = [
                k[0] if len(k) == 1 else f"({', '.join(k)})" for k in _unique_keys(table)
            ]
            if alternates:
                parts.append(f"unique: {', '.join(alternates)}")
        suffix = f" ({'; '.join(parts)})" if parts else ""
        kind = " [view]" if table.kind == "view" else ""
        entries.append(f"- {table.qualified_name}{kind}{suffix}")
        columns[table.qualified_name] = {c.name for c in table.columns}
    return CatalogIndex(database=snapshot.database, entries=entries, columns=columns)


def _check_columns(definition: str | None, names: dict[str, str]) -> list[str]:
    """Columns referenced by a T-SQL check definition.

    ``sys.check_constraints.definition`` always brackets identifiers, e.g.
    ``([status]='A' OR [status]='B')``. The Phase 1 reader does not record the
    parent column, so it is recovered here. Result is in definition order.
    """
    if not definition:
        return []
    found: list[str] = []
    for token in _BRACKETED.findall(definition):
        actual = names.get(token.lower())
        if actual and actual not in found:
            found.append(actual)
    return found


def _drop_empty(entry: dict[str, Any]) -> dict[str, Any]:
    """Omit null/false/empty values so the model sees only real facts."""
    return {k: v for k, v in entry.items() if v not in (None, False, [], "")}


def build_bundle(table: TableSnapshot, dialect: str = "mssql") -> TableBundle:
    """``dialect`` selects the type vocabulary the scorer uses; it does not
    change the rendered document, so a SQL Server bundle is byte-identical to
    Phase 2's."""
    table = table.sorted_view()
    names = {c.name.lower(): c.name for c in table.columns}
    facts: dict[str, ColumnFacts] = {
        c.name: ColumnFacts(
            name=c.name,
            data_type=c.data_type,
            nullable=c.nullable,
            has_comment=bool(c.comment and c.comment.strip()),
            has_default=c.default is not None,
            is_identity=c.is_identity,
            is_computed=c.is_computed,
            is_rowguid=c.is_rowguidcol,
            dialect=dialect,
            type_definition=c.type_definition,
        )
        for c in table.columns
    }
    ref_columns: dict[str, set[str]] = {}

    def link(ref: str, cols: list[str]) -> None:
        ref_columns[ref] = set(cols)
        for col in cols:
            if col in facts:
                facts[col].refs.add(ref)

    columns_doc = [
        # name/type/nullable always shown (nullable even when false); the rest
        # only when the fact exists.
        {"name": c.name, "type": c.data_type, "nullable": c.nullable}
        | _drop_empty(
            {
                "default": c.default,
                "identity": c.is_identity,
                "computed": c.computed_definition if c.is_computed else None,
                "comment": c.comment,
                "sparse": c.is_sparse,
                "rowguid": c.is_rowguidcol,
                "user_defined_type": c.is_user_defined_type,
                "type_definition": c.type_definition,
            }
        )
        for c in table.columns
    ]

    primary_key: dict[str, Any] | None = None
    unique_constraints: list[dict[str, Any]] = []
    check_constraints: list[dict[str, Any]] = []

    for con in table.constraints:
        if con.kind == "primary_key":
            ref = f"pk:{con.name}"
            link(ref, con.columns)
            for col in con.columns:
                if col in facts:
                    facts[col].in_primary_key = True
                    facts[col].primary_key_size = len(con.columns)
            primary_key = {"ref": ref, "columns": con.columns}
        elif con.kind == "unique":
            ref = f"uq:{con.name}"
            link(ref, con.columns)
            for col in con.columns:
                if col in facts:
                    facts[col].in_unique = True
            unique_constraints.append({"ref": ref, "columns": con.columns})
        elif con.kind == "check":
            ref = f"ck:{con.name}"
            cols = con.columns or _check_columns(con.definition, names)
            link(ref, cols)
            enforced = not con.is_disabled and not con.is_not_trusted
            for col in cols:
                if col in facts:
                    facts[col].checks.append(ref)
                    facts[col].checks_trusted = facts[col].checks_trusted or enforced
            check_constraints.append(
                _drop_empty(
                    {
                        "ref": ref,
                        "columns": cols,
                        "definition": con.definition,
                        "disabled": con.is_disabled,
                        "not_trusted": con.is_not_trusted,
                    }
                )
            )
        # "default" constraints are shown on the column itself.

    indexes: list[dict[str, Any]] = []
    for idx in table.indexes:
        if idx.is_primary_key or idx.is_unique_constraint:
            continue  # already shown as primary_key / unique_constraints
        keys = [c["name"] for c in idx.columns if not c.get("included")]
        included = [c["name"] for c in idx.columns if c.get("included")]
        ref = f"ix:{idx.name}"
        link(ref, keys + included)
        if idx.is_unique and not idx.filter_definition and not idx.is_disabled:
            for col in keys:
                if col in facts:
                    facts[col].in_unique = True
        indexes.append(
            _drop_empty(
                {
                    "ref": ref,
                    "type": idx.type_desc.lower(),
                    "unique": idx.is_unique,
                    "columns": keys,
                    "included": included,
                    "filter": idx.filter_definition,
                    "disabled": idx.is_disabled,
                }
            )
        )

    foreign_keys: list[dict[str, Any]] = []
    for rel in table.relations:
        ref = f"fk:{rel.name}"
        link(ref, rel.from_columns)
        enforced = not rel.is_disabled and not rel.is_not_trusted
        for col in rel.from_columns:
            if col in facts:
                facts[col].foreign_keys.append(ref)
                facts[col].foreign_keys_trusted = facts[col].foreign_keys_trusted or enforced
        on_delete = rel.on_delete if rel.on_delete and rel.on_delete != "NO_ACTION" else None
        on_update = rel.on_update if rel.on_update and rel.on_update != "NO_ACTION" else None
        foreign_keys.append(
            _drop_empty(
                {
                    "ref": ref,
                    "columns": rel.from_columns,
                    "references": f"{rel.to_schema}.{rel.to_table}",
                    "referenced_columns": rel.to_columns,
                    "on_delete": on_delete,
                    "on_update": on_update,
                    "disabled": rel.is_disabled,
                    "not_trusted": rel.is_not_trusted,
                }
            )
        )

    document: dict[str, Any] = {
        "table": table.qualified_name,
        "kind": table.kind,
        "comment": table.comment,
        "estimated_rows": table.estimated_rows,
        "columns": columns_doc,
        "primary_key": primary_key,
        "unique_constraints": unique_constraints,
        "check_constraints": check_constraints,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
    }
    document = {k: v for k, v in document.items() if v not in (None, [])}

    return TableBundle(
        schema=table.schema,
        name=table.name,
        kind=table.kind,
        structure_hash=table.structure_hash,
        document=document,
        facts=facts,
        ref_columns=ref_columns,
        has_comment=bool(table.comment and table.comment.strip()),
    )
