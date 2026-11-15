"""View column lineage from view definitions (Phase 3A, premise A8).

Each column of a view is classified:

* ``pass_through`` — a plain (possibly renamed) column of one base table. Its
  meaning is the base column's; if the view renames it, the new name is an
  *alias* — evidence for the base column, often the only business name a
  worst-case database has (``f7 AS amount``).
* ``computed`` — an expression, aggregate or ``CASE``; its inputs are listed.
* ``unresolved`` — the definition could not be parsed or mapped (set
  operations, table functions, encrypted or dynamic modules); the reason is kept.

Parsing uses ``sqlglot`` with the target's dialect and the catalog as schema,
so unqualified columns resolve to their tables. Only the outermost ``SELECT``
is mapped; columns that come from sub-queries or CTEs are ``unresolved`` with
that reason rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

__all__ = ["ColumnLineage", "parse_view_lineage", "sqlglot_dialect"]


@dataclass(frozen=True)
class ColumnLineage:
    view: str  # schema.view
    column: str
    classification: str  # pass_through | computed | unresolved
    source: str | None = None  # schema.table.column for pass_through
    expression: str | None = None  # for computed
    inputs: tuple[str, ...] = field(default_factory=tuple)  # schema.table.column
    detail: str = ""
    # A computed column that only converts one base column (cast, to_date,
    # trim, coalesce with a constant): its name is an alias of that column too.
    converts: str | None = None  # schema.table.column

    @property
    def source_table(self) -> str | None:
        return self.source.rsplit(".", 1)[0] if self.source else None

    @property
    def source_column(self) -> str | None:
        return self.source.rsplit(".", 1)[1] if self.source else None


def sqlglot_dialect(dialect: str) -> str:
    return "tsql" if dialect == "mssql" else "postgres"


def _select_of(definition: str, dialect: str) -> exp.Select | exp.Expression:
    tree = sqlglot.parse_one(definition, read=sqlglot_dialect(dialect))
    if isinstance(tree, exp.Create):
        tree = tree.expression
    return tree


# Nodes that change what a value means: aggregates, windows, conditionals,
# arithmetic, comparisons and concatenation (all ``exp.Binary``), sub-queries.
_MEANING_CHANGERS = (
    exp.AggFunc,
    exp.Window,
    exp.Case,
    exp.If,
    exp.Binary,
    exp.Predicate,
    exp.Subquery,
    exp.Select,
)


def _only_converts(node: exp.Expression) -> bool:
    """True for casts and conversion functions of one column, e.g.
    ``to_date(f3::text, 'YYYYMMDD')`` or ``CONVERT(date, f3, 112)``."""
    return not any(isinstance(n, _MEANING_CHANGERS) for n in node.walk())


def parse_view_lineage(
    view: str,
    view_columns: list[str],
    definition: str | None,
    dialect: str,
    schema: dict[str, dict[str, dict[str, str]]],
) -> list[ColumnLineage]:
    """Lineage for every column of ``view`` (in ``view_columns`` order).

    ``schema`` is ``{schema: {table: {column: type}}}`` from the catalog snapshot.
    """

    def unresolved(reason: str) -> list[ColumnLineage]:
        return [ColumnLineage(view, c, "unresolved", detail=reason) for c in view_columns]

    if not definition or not definition.strip():
        return unresolved("no definition (encrypted module or no VIEW DEFINITION)")
    try:
        select = _select_of(definition, dialect)
        if not isinstance(select, exp.Select):
            return unresolved(f"outermost statement is {type(select).__name__}, not a SELECT")
        select = qualify(
            select,
            schema=schema,
            dialect=sqlglot_dialect(dialect),
            validate_qualify_columns=False,
            quote_identifiers=False,
        )
    except Exception as exc:  # sqlglot raises several error types
        return unresolved(f"definition not parsed: {type(exc).__name__}: {str(exc)[:160]}")

    # alias -> "schema.table" for real tables in the outermost FROM/JOINs.
    tables: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Subquery) is not None:
            continue
        if table.db:
            tables[table.alias_or_name] = f"{table.db}.{table.name}"

    def qualified(column: exp.Column) -> str | None:
        owner = tables.get(column.table)
        return f"{owner}.{column.name}" if owner else None

    projections = select.expressions
    out: list[ColumnLineage] = []
    by_name = {p.alias_or_name: p for p in projections}
    for position, name in enumerate(view_columns):
        node = by_name.get(name)
        if node is None and position < len(projections):
            node = projections[position]  # e.g. CREATE VIEW v (a, b) AS SELECT ...
        if node is None:
            out.append(ColumnLineage(view, name, "unresolved", detail="no matching projection"))
            continue
        inner = node.this if isinstance(node, exp.Alias) else node
        if isinstance(inner, exp.Column):
            source = qualified(inner)
            if source:
                out.append(ColumnLineage(view, name, "pass_through", source=source))
            else:
                out.append(
                    ColumnLineage(
                        view,
                        name,
                        "unresolved",
                        detail=f"column {inner.sql()} comes from a sub-query",
                    )
                )
            continue
        inputs = tuple(
            dict.fromkeys(q for q in (qualified(c) for c in inner.find_all(exp.Column)) if q)
        )
        out.append(
            ColumnLineage(
                view,
                name,
                "computed",
                expression=inner.sql(dialect=sqlglot_dialect(dialect))[:500],
                inputs=inputs,
                converts=inputs[0] if len(inputs) == 1 and _only_converts(inner) else None,
            )
        )
    return out
