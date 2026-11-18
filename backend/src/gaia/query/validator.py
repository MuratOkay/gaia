"""The SQL guard: validate one generated statement before it reaches a target (Phase 4A).

Dialect-independent machinery; each dialect supplies its rules
(``dialects/<d>/guard.py``). The guard works on the ``sqlglot`` AST and returns
the statement **rendered from that AST** — the executor never sees the text it
was given, only what was validated. A construct the parser misunderstands can
therefore only become something harmless (e.g. an alias), never a hidden
second statement.

Checks, in order (PHASES, Phase 4 "Validation rules (non-negotiable)"):

1. exactly one statement, and it parses;
2. the statement is a query: ``SELECT`` (with or without ``WITH``) or a set
   operation of queries — an **allowlist** of root types, because unknown
   syntax parses as ``Command``, ``Alias`` or ``Column`` roots;
3. nothing anywhere in the tree writes, locks or changes the session: no
   DML/DDL (including writable CTEs), ``SELECT … INTO``, ``FOR UPDATE``,
   variables or parameters;
4. every object is in the catalog snapshot (so never a system catalog), each
   reference resolved to one schema and qualified — PostgreSQL sessions run
   with ``search_path = pg_catalog``;
5. every column resolves (``sqlglot``'s qualifier against the snapshot);
6. every function is on the dialect's **allowlist**, by the name the target
   will see (the rendered name, so a parser version that types a function
   differently changes nothing); no schema-qualified (user) functions;
7. every join is backed by a join path (``query.catalog``): a declared FK, a
   tested model hint (D48) or an SME hint — through view lineage, directly or
   through a shared key — **and one side of it is unique** (a key, or a derived
   table grouped by that column), or the join multiplies rows (the fan trap).
   Joins without a condition (comma, ``CROSS``) and ``NATURAL`` joins are not
   backed. Nor is the **chasm trap** (D90): two tables that each hold many rows per
   row of a third, joined through it in one query whose sums or counts read them
   (order lines and payments through their order) — each join is backed, yet the
   rows multiply. ``query.unverified_joins: warn`` downgrades this one rule;
8. a row cap: ``LIMIT`` / ``TOP`` / ``FETCH`` is added, or lowered to the cap;
9. the rendered statement is parsed again and checks 1–3 and 6 re-run on it:
   what executes is exactly what passed.

The read-only login and the read-only session remain the boundary (D4); this
is the second, independent layer. Prompts are never a guard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from gaia.query.catalog import GuardCatalog

__all__ = ["GuardResult", "JoinCheck", "SqlGuardBase", "Violation"]

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # unsupported syntax is rejected anyway

# Statement roots that read. Everything else — including what the parser could
# not understand (Command, Alias, Column …) — is rejected.
_ROOTS = tuple(getattr(exp, n) for n in ("Select", "Union", "Intersect", "Except")
               if hasattr(exp, n))

# Nodes that write, change the session or run other code; looked up by name so a
# sqlglot version without one of them still loads.
_FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
        "Command", "TruncateTable", "Copy", "Set", "Execute", "Use", "Declare", "Grant",
        "Revoke", "Transaction", "Commit", "Rollback", "LoadData", "Pragma", "Kill",
        "Analyze", "Refresh", "Cache", "Uncache", "Into", "Lock", "Values",
    ) if hasattr(exp, n)
)
_VARIABLES = tuple(getattr(exp, n) for n in ("Parameter", "SessionParameter", "Placeholder")
                   if hasattr(exp, n))
_CALL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
# Rendered function-like syntax that is not a call of a named function.
_SYNTAX = frozenset({"cast", "try_cast", "extract", "case", "exists", "any", "all", "some",
                     "array", "row", "in", "filter", "over", "within", "interval"})


@dataclass(frozen=True)
class Violation:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class JoinCheck:
    """One join of the statement and what backs it."""

    left: str | None  # schema.table.column (resolved through CTEs and subqueries)
    right: str | None
    # fk | hint | sme (…_via_view) | same_column | shared_key | generated — verified;
    # unverified | fan_out | cross | natural | chasm — not
    kind: str
    sql: str  # the join as written

    @property
    def verified(self) -> bool:
        return self.kind not in ("unverified", "fan_out", "cross", "natural", "chasm")


@dataclass
class GuardResult:
    dialect: str
    submitted: str
    sql: str | None = None  # what may be executed (rendered from the validated AST)
    errors: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)  # schema.object read
    joins: list[JoinCheck] = field(default_factory=list)
    row_limit: int | None = None
    limit_injected: bool = False
    # the statement's output columns and the base column each one shows as is
    # (``schema.object.column``), None when computed — for masking personal data
    outputs: list[tuple[str, str | None]] = field(default_factory=list)
    # need-to-know (5C): what a ``forbidden`` statement reached for — for the audit only,
    # never for the person who asked
    forbidden: list[str] = field(default_factory=list)
    # whether the outermost query orders its rows (D90: an unordered result longer than the
    # narrative may see is sorted before it is cut, so every server shows the same rows)
    ordered: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and self.sql is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "dialect": self.dialect, "sql": self.sql,
            "errors": [v.__dict__ for v in self.errors],
            "warnings": [v.__dict__ for v in self.warnings],
            "tables": self.tables,
            "joins": [j.__dict__ for j in self.joins],
            "row_limit": self.row_limit, "limit_injected": self.limit_injected,
            **({"forbidden": self.forbidden} if self.forbidden else {}),
        }


class SqlGuardBase:
    """Validation common to all dialects; subclasses set the class attributes
    and may extend ``dialect_checks`` and ``apply_limit``."""

    dialect: str = ""
    read: str = ""  # sqlglot dialect
    functions: frozenset[str] = frozenset()  # allowed function names, lower case
    denied_function_classes: frozenset[str] = frozenset()
    # need-to-know (5C): ``*`` is expanded by the database, not by the guard — refused when
    # the catalog is a person's restricted one
    forbid_star: bool = False

    def __init__(self, catalog: GuardCatalog, *, row_limit: int = 1000,
                 unverified_joins: str = "reject") -> None:
        if unverified_joins not in ("reject", "warn"):
            raise ValueError("unverified_joins must be 'reject' or 'warn'")
        self.catalog = catalog
        self.row_limit = max(1, int(row_limit))
        self.unverified_joins = unverified_joins

    # -- SqlGuard protocol ---------------------------------------------------------------

    def validate(self, sql: str) -> list[str]:
        return [str(v) for v in self.check(sql).errors]

    def check(self, sql: str) -> GuardResult:
        result = GuardResult(self.dialect, sql)
        tree = self._parse(sql, result)
        if tree is None:
            return result
        result.errors += self._structure(tree)
        if result.errors:
            return result
        if self.forbid_star:
            result.errors += _stars(tree, self.read)
        result.errors += self.dialect_checks(tree)
        result.errors += self._functions(tree)
        result.errors += self._resolve_tables(tree, result)
        if result.errors:
            return result
        self._natural_joins(tree, result)  # before qualify, which makes them explicit
        if result.errors:
            return result
        qualified = self._qualify(tree, result)
        if qualified is None:
            return result
        self._check_joins(qualified, result)
        if result.errors:
            return result
        if self.catalog.show_only:
            result.errors += _show_only(qualified, self.catalog.show_only)
            if result.errors:
                return result
        result.outputs = _outputs(qualified)
        result.ordered = tree.args.get("order") is not None
        tree, result.row_limit, result.limit_injected = self.apply_limit(tree)
        rendered = tree.sql(dialect=self.read, pretty=True)
        # 9. what executes is what passed: parse the rendering and check it again
        again = self._parse(rendered, result)
        if again is None:
            return result
        result.errors += [Violation(v.code, f"after rendering: {v.message}")
                          for v in self._structure(again) + self._functions(again)]
        if not result.errors:
            result.sql = rendered
        return result

    # -- hooks -----------------------------------------------------------------------------

    def dialect_checks(self, tree: exp.Expression) -> list[Violation]:
        return []

    def apply_limit(self, tree: exp.Expression) -> tuple[exp.Expression, int, bool]:
        """Cap the rows of the outermost query: (statement, effective cap, whether the cap
        was added or lowered). A set operation may come back wrapped in a query."""
        cap = self.row_limit
        limit = tree.args.get("limit")
        if isinstance(limit, exp.Fetch):
            count = limit.args.get("count")
            n = _int(count)
            if n is not None and n <= cap and not _percent(limit):
                return tree, n, False
            limit.set("count", exp.Literal.number(cap))
            _clear_percent(limit)
            return tree, cap, True
        if isinstance(limit, exp.Limit):
            n = _int(limit.expression)
            if n is not None and n <= cap and not _percent(limit):
                return tree, n, False
            limit.set("expression", exp.Literal.number(cap))
            _clear_percent(limit)
            return tree, cap, True
        return tree.limit(cap, copy=False), cap, True

    # -- steps -----------------------------------------------------------------------------

    def _parse(self, sql: str, result: GuardResult) -> exp.Expression | None:
        try:
            trees = [t for t in sqlglot.parse(sql, read=self.read) if t is not None]
        except (ParseError, SqlglotError) as exc:
            result.errors.append(Violation("parse_error", _first_line(str(exc))))
            return None
        if not trees:
            result.errors.append(Violation("empty", "no statement"))
            return None
        if len(trees) > 1:
            result.errors.append(Violation(
                "multiple_statements", f"{len(trees)} statements; exactly one is allowed"))
            return None
        tree = trees[0]
        while isinstance(tree, exp.Subquery) and not tree.alias:  # "(SELECT …)"
            tree = tree.this
        return tree

    def _structure(self, tree: exp.Expression) -> list[Violation]:
        if not isinstance(tree, _ROOTS):
            return [Violation("statement_type",
                              f"only SELECT / WITH queries are allowed, not {_kind(tree)}")]
        out = []
        for node in tree.walk():
            node = node[0] if isinstance(node, tuple) else node  # sqlglot < 26 yields tuples
            if isinstance(node, _FORBIDDEN_NODES):
                if isinstance(node, exp.Into):
                    out.append(Violation("select_into", "SELECT … INTO creates a table"))
                elif isinstance(node, exp.Lock):
                    out.append(Violation("locking_clause",
                                         f"row locks are not allowed: {node.sql(self.read)}"))
                elif isinstance(node, exp.Values):
                    continue  # VALUES lists are harmless inside a query; roots are checked
                else:
                    out.append(Violation("write_operation",
                                         f"{_kind(node)} is not allowed inside a query"))
            elif isinstance(node, _VARIABLES):
                out.append(Violation("variable",
                                     f"variables and parameters are not allowed: "
                                     f"{node.sql(self.read)}"))
        return out

    def _function_name(self, node: exp.Func) -> str | None:
        if isinstance(node, exp.Anonymous):
            return str(node.name).lower()
        m = _CALL.match(node.sql(dialect=self.read))
        return m.group(1).lower() if m else None

    def _functions(self, tree: exp.Expression) -> list[Violation]:
        out, seen = [], set()
        for node in tree.find_all(exp.Func):
            if type(node).__name__ in self.denied_function_classes:
                out.append(Violation("function_not_allowed",
                                     f"{node.sql(self.read)[:60]} is not allowed"))
                continue
            if isinstance(node.parent, exp.Dot) and node.parent.expression is node:
                out.append(Violation("qualified_function",
                                     f"schema-qualified (user) functions are not allowed: "
                                     f"{node.parent.sql(self.read)[:60]}"))
                continue
            name = self._function_name(node)
            if name is None or name in _SYNTAX or name in self.functions or name in seen:
                continue
            seen.add(name)
            out.append(Violation("function_not_allowed",
                                 f"function {name}() is not on the {self.dialect} allowlist"))
        return out

    def _cte_names(self, tree: exp.Expression) -> set[str]:
        return {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    def _resolve_tables(self, tree: exp.Expression, result: GuardResult) -> list[Violation]:
        out: list[Violation] = []
        ctes = self._cte_names(tree)
        read: list[str] = []
        for table in list(tree.find_all(exp.Table)):
            if not isinstance(table.this, exp.Identifier):
                if isinstance(table.this, exp.Func):
                    if table.db:
                        out.append(Violation("qualified_function",
                                             f"schema-qualified (user) functions are not "
                                             f"allowed: {table.sql(self.read)[:60]}"))
                    continue  # a table function: checked with the functions
                out.append(Violation("object_name",
                                     f"unsupported object reference {table.sql(self.read)}"))
                continue
            name, schema = table.name, table.db or None
            catalog = table.catalog or None
            if catalog is not None:
                if catalog.lower() != (self.catalog.database or "").lower():
                    out.append(Violation("other_database",
                                         f"{table.sql(self.read)}: only objects of "
                                         f"{self.catalog.database} may be read"))
                    continue
                table.set("catalog", None)
            if schema is None and name.lower() in ctes:
                continue
            hits = self.catalog.find(schema, name)
            label = f"{schema}.{name}" if schema else name
            if not hits:
                out.append(Violation("unknown_object", f"{label} is not in the catalog"))
                continue
            if len(hits) > 1:
                options = ", ".join(f"{s}.{o}" for s, o in hits)
                out.append(Violation("ambiguous_object",
                                     f"{label} matches {options}; qualify it"))
                continue
            s, o = hits[0]
            table.set("db", exp.to_identifier(s, quoted=_needs_quotes(s)))
            table.set("this", exp.to_identifier(o, quoted=_needs_quotes(o)))
            read.append(f"{s}.{o}")
        result.tables = sorted(set(read))
        return out

    def _schema(self) -> MappingSchema:
        mapping = {s: {o: {c: "text" for c in cols} for o, cols in objs.items()}
                   for s, objs in self.catalog.objects.items()}
        return MappingSchema(mapping, dialect=self.read)

    def _qualify(self, tree: exp.Expression, result: GuardResult) -> exp.Expression | None:
        try:
            return qualify(tree.copy(), schema=self._schema(), dialect=self.read,
                           validate_qualify_columns=True, quote_identifiers=False,
                           identify=False)
        except SqlglotError as exc:
            message = _first_line(str(exc))
            unknown = "could not be resolved" in message or "Unknown column" in message
            code = "unknown_column" if unknown else "unresolved"
            result.errors.append(Violation(code, message))
            return None

    def _natural_joins(self, tree: exp.Expression, result: GuardResult) -> None:
        for join in tree.find_all(exp.Join):
            if str(join.args.get("method") or "").upper() == "NATURAL":
                self._unverified(result, JoinCheck(None, None, "natural",
                                                   join.sql(self.read)))

    def _unverified(self, result: GuardResult, check: JoinCheck) -> None:
        result.joins.append(check)
        message = {
            "cross": "a join without a condition (comma or CROSS JOIN)",
            "natural": "a NATURAL join (its columns are implicit)",
            "fan_out": "neither side of this join is unique, so it multiplies rows (a fan "
                       "trap): aggregate one side by the join column first",
            "chasm": f"{check.left} and {check.right} both hold many rows per row of the "
                     "table they are joined through, so every row of one is repeated for "
                     "each row of the other and sums and counts grow (a chasm trap): "
                     "aggregate each of them by that key in a WITH first, then join the "
                     "results",
        }.get(check.kind, "a join condition with no known join path "
                          "(declared FK, tested hint, SME hint or view lineage)")
        violation = Violation("unverified_join", f"{message}: {check.sql[:120]}")
        (result.warnings if self.unverified_joins == "warn" else result.errors
         ).append(violation)

    def _check_joins(self, qualified: exp.Expression, result: GuardResult) -> None:
        """Every join of every scope (in warn mode a NATURAL join is listed twice: as
        written, and as the explicit join the qualifier made of it); then, per scope, the
        chasm trap."""
        for scope in traverse_scope(qualified):
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            for join in select.args.get("joins") or []:
                check = self._join(scope, join)
                if check.verified:
                    result.joins.append(check)
                else:
                    self._unverified(result, check)
            chasm = self._chasm(scope, select)
            if chasm is not None:
                self._unverified(result, chasm)

    def _chasm(self, scope: Scope, select: exp.Select) -> JoinCheck | None:
        """Two different sources of one scope each joined from their *many* side to the same
        source — and a sum, average or plain count reading the scope's rows (here or in a
        query around it). MIN, MAX and COUNT(DISTINCT …) are not changed by repeated rows."""
        parents: dict[str, list[tuple[str, str]]] = {}  # one-side alias -> (many alias, text)
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(exp.EQ):
                a, b = eq.this, eq.expression
                if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
                    continue
                (left, lu), (right, ru) = _resolve(scope, a), _resolve(scope, b)
                if not left or not right or GENERATED in (left, right):
                    continue
                if not self.catalog.join_kind(left, right):
                    continue
                left_one = lu or self.catalog.is_unique(left)
                right_one = ru or self.catalog.is_unique(right)
                if left_one == right_one:
                    continue  # one to one, or a fan-out (refused on its own)
                one, many = (a, b) if left_one else (b, a)
                if one.table == many.table:
                    continue
                label = _source_label(scope, many.table)
                parents.setdefault(one.table, [])
                if all(m != many.table for m, _l in parents[one.table]):
                    parents[one.table].append((many.table, label))
                    if len(parents[one.table]) >= 2 and _aggregates_rows(scope):
                        (_m1, first), (_m2, second) = parents[one.table][:2]
                        return JoinCheck(first, second, "chasm", join.sql(self.read))
        return None

    def _join(self, scope: Scope, join: exp.Join) -> JoinCheck:
        text = join.sql(self.read)
        on = join.args.get("on")
        if on is None:
            return JoinCheck(None, None, "cross", text)
        first: tuple[str | None, str | None] = (None, None)
        fan_out: JoinCheck | None = None
        for eq in on.find_all(exp.EQ):
            a, b = eq.this, eq.expression
            if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
                continue
            (left, lu), (right, ru) = _resolve(scope, a), _resolve(scope, b)
            if GENERATED in (left, right):
                # a generated series or a VALUES list: not a relationship between tables
                return JoinCheck(left, right, "generated", text)
            if first == (None, None):
                first = (left, right)
            if left and right:
                kind = self.catalog.join_kind(left, right)
                if kind:
                    if lu or ru or self.catalog.is_unique(left) or self.catalog.is_unique(right):
                        return JoinCheck(left, right, kind, text)
                    fan_out = fan_out or JoinCheck(left, right, "fan_out", text)
        return fan_out or JoinCheck(first[0], first[1], "unverified", text)


GENERATED = "<generated>"  # a column of a table function or a VALUES list

# aggregates that repeated rows change (the chasm trap); MIN, MAX and COUNT(DISTINCT) do not
_ROW_SENSITIVE = (exp.Sum, exp.Avg, exp.Count, exp.Stddev, exp.StddevPop, exp.StddevSamp,
                  exp.Variance, exp.VariancePop)


def _row_sensitive(select: exp.Expression) -> bool:
    for agg in select.find_all(*_ROW_SENSITIVE):
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct):
            continue
        # only this query's own aggregates, not those of a subquery inside it
        if agg.find_ancestor(exp.Select) is select:
            return True
    return False


def _aggregates_rows(scope: Scope) -> bool:
    """Whether the rows of ``scope`` feed a sum, average or count — its own, or of a query
    it is a derived table or CTE of."""
    current: Scope | None = scope
    while current is not None:
        if isinstance(current.expression, exp.Select) and _row_sensitive(current.expression):
            return True
        current = current.parent
    return False


def _source_label(scope: Scope, alias: str) -> str:
    source = scope.sources.get(alias)
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
        return f"{source.db}.{source.name}"
    return alias


def _stars(tree: exp.Expression, read: str) -> list[Violation]:
    """``*`` and ``t.*`` outside ``COUNT(*)``."""
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Count):
            continue
        return [Violation("star", "name the columns instead of * (it would also return "
                                  "columns this person may not read)")]
    return []


def _show_only(qualified: exp.Expression, show_only: set[str]) -> list[Violation]:
    """Personal data a person may see only masked (5C): a reference to such a column is
    allowed as an output column (of the query, a CTE or a derived table — its use further
    out is checked where it is used), inside ``COUNT(…)``, or tested with ``IS [NOT] NULL``
    (whether there is a value, not what it is); anywhere else — a filter, a
    join, a grouping, an ordering, a window, an expression, a subquery in a condition — it
    would reveal what the mask hides."""
    try:
        scopes = traverse_scope(qualified)
    except SqlglotError:
        return [Violation("unresolved", "the statement could not be analysed")]
    for scope in scopes:
        select = scope.expression
        aliases: set[str] = set()
        if isinstance(select, exp.Select):
            for projection in select.expressions:
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                if isinstance(inner, exp.Column):
                    base = _resolve(scope, inner)[0]
                    if base and base.lower() in show_only:
                        aliases.add(projection.alias_or_name.lower())
        for column in scope.columns:
            if column.table:
                base, outer = None, scope
                while outer is not None:  # a correlated reference reads an outer source
                    if column.table in outer.sources:
                        base = _resolve(outer, column)[0]
                        break
                    outer = outer.parent
                if not base or base.lower() not in show_only:
                    continue
            elif column.name.lower() not in aliases:
                continue  # an unqualified name: an output alias, if anything
            if _shown_or_counted(column, scope):
                continue
            return [Violation("personal_data",
                              "personal data may only be shown (masked) or counted here: "
                              f"{column.sql()[:60]}")]
    return []


def _shown_or_counted(column: exp.Column, scope: Scope) -> bool:
    parent = column.parent
    if isinstance(parent, exp.Is) and isinstance(parent.expression, exp.Null):
        return True  # whether there is a value at all ("has an e-mail") reveals no value
    if isinstance(parent, exp.Distinct):
        parent = parent.parent
    if isinstance(parent, exp.Count):
        return True
    if isinstance(parent, exp.Alias):
        parent = parent.parent
    projection = isinstance(parent, exp.Select) and parent is scope.expression and (
        column in parent.expressions or column.parent in parent.expressions)
    return projection and not scope.is_subquery


def _resolve(scope: Scope, column: exp.Column, depth: int = 0) -> tuple[str | None, bool]:
    """(``schema.table.column`` a column reads — through CTEs and subqueries —,
    whether the query makes it unique there). ``GENERATED`` for a table function or
    VALUES list (treated as unique); None when the value is computed.

    A derived table makes a column unique when it is grouped by exactly that
    column, or selects only that column ``DISTINCT``; a plain filter over one table
    keeps the base column's uniqueness (the catalog decides that one)."""
    if depth > 20:
        return None, False
    source = scope.sources.get(column.table)
    if isinstance(source, exp.Table):
        if not isinstance(source.this, exp.Identifier):
            return GENERATED, True
        return f"{source.db}.{source.name}.{column.name}", False
    if isinstance(source, Scope):
        select = source.expression
        set_operation = isinstance(select, (exp.Union, exp.Intersect, exp.Except))
        while isinstance(select, (exp.Union, exp.Intersect, exp.Except)):
            select = select.this  # the first branch names the columns
        if isinstance(select, exp.Values):
            return GENERATED, True
        if not isinstance(select, exp.Select):
            return None, False
        for projection in select.expressions:
            if projection.alias_or_name != column.name:
                continue
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(inner, exp.Column):
                return None, False
            unique = not set_operation and _groups_by_only(select, inner)
            branch = source if source.expression is select else _branch_scope(source, select)
            if branch is None:
                return None, False
            base, inner_unique = _resolve(branch, inner, depth + 1)
            plain = not (select.args.get("joins") or select.args.get("group")
                         or set_operation)
            return base, unique or (inner_unique and plain)
    return None, False


def _groups_by_only(select: exp.Select, column: exp.Column) -> bool:
    """The derived table has one row per value of ``column``."""
    group = select.args.get("group")
    if group is not None:
        keys = group.expressions
        return (len(keys) == 1 and isinstance(keys[0], exp.Column)
                and keys[0].name == column.name and keys[0].table == column.table)
    distinct = select.args.get("distinct")
    return distinct is not None and not distinct.args.get("on") and len(select.expressions) == 1


def _outputs(qualified: exp.Expression) -> list[tuple[str, str | None]]:
    """(name, base column shown as is) for the outermost query's columns."""
    try:
        root = build_scope(qualified)
    except SqlglotError:
        return []
    if root is None:
        return []
    select = qualified
    while isinstance(select, (exp.Union, exp.Intersect, exp.Except)):
        select = select.this
    scope = root
    if select is not qualified:
        scope = next((s for s in traverse_scope(qualified) if s.expression is select), None)
    if not isinstance(select, exp.Select) or scope is None:
        return []
    out = []
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        base = _resolve(scope, inner)[0] if isinstance(inner, exp.Column) else None
        out.append((projection.alias_or_name, base if base != GENERATED else None))
    return out


def _branch_scope(scope: Scope, select: exp.Select) -> Scope | None:
    for child in scope.union_scopes or []:
        if child.expression is select:
            return child
    return None


def _percent(limit: exp.Expression) -> bool:
    """``TOP (n) PERCENT`` / ``FETCH … n PERCENT``: where sqlglot keeps the flag
    differs between versions."""
    options = limit.args.get("limit_options")
    return bool(limit.args.get("percent") or (options is not None and options.args.get("percent")))


def _clear_percent(limit: exp.Expression) -> None:
    if limit.args.get("percent"):
        limit.set("percent", None)
    options = limit.args.get("limit_options")
    if options is not None and options.args.get("percent"):
        options.set("percent", False)


def _int(node: Any) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.name)
        except ValueError:
            return None
    return None


def _kind(node: exp.Expression) -> str:
    if isinstance(node, exp.Command):
        return f"{str(node.this).upper()} (command)"
    return type(node).__name__.upper()


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0][:300] if text.strip() else text


def _needs_quotes(name: str) -> bool:
    return not re.fullmatch(r"[a-z_][a-z0-9_]*", name)
