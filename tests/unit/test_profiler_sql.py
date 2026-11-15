"""Every statement the profilers generate, for both dialects, parsed with sqlglot.

PHASES 3A acceptance: *no generated profiler statement contains an unordered
``TOP`` used as a sample*. The rule, checked on the syntax tree of every
statement: a row cap (``TOP`` / ``LIMIT``) is only legal on an ordered query
(top-k) or over a ``TABLESAMPLE`` source. Also: every statement is a ``SELECT``
(no DML, DDL or commands), and SQL Server statements carry ``MAXDOP``.

SQL Server cannot be run here; parsing with ``read="tsql"`` is the offline check,
the live one is the owner's ``make profile TARGET=demo_mssql``.
"""

from __future__ import annotations

import _fake_pyodbc as fake
import pytest
import sqlglot
from sqlglot import exp

fake.install()

from gaia.core.config import ProfilingBudget, TargetConfig  # noqa: E402
from gaia.dialects.mssql.profiler import MssqlProfiler  # noqa: E402
from gaia.dialects.postgres.profiler import PostgresProfiler  # noqa: E402

COLUMNS = {
    "postgres": [("id", "integer"), ("code", "varchar(12)"), ("note", "text"),
                 ("amount", "numeric(12,2)"), ("flag", "boolean"), ("ts", "timestamptz"),
                 ("doc", "jsonb"), ("tags", "text[]"), ("guid", "uuid"), ("raw", "bytea")],
    "mssql": [("id", "int"), ("code", "nvarchar(12)"), ("note", "nvarchar(max)"),
              ("amount", "decimal(12,2)"), ("flag", "bit"), ("ts", "datetime2(7)"),
              ("doc", "xml"), ("guid", "uniqueidentifier"), ("raw", "varbinary(max)"),
              ("old", "ntext")],
}
DDL_OR_DML = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
              exp.Command, exp.TruncateTable)


class _Cursor:
    """Records statements; answers nothing."""

    def __init__(self, log):
        self.log = log
        self.description = [("x",)]

    def execute(self, sql, *params):
        self.log.append(sql)

    def fetchall(self):
        return []

    def fetchmany(self, n):
        return []


def _profiler(dialect: str):
    target = TargetConfig(dialect=dialect, host="h", database="d", username="u", password="p")
    cls = PostgresProfiler if dialect == "postgres" else MssqlProfiler
    return cls(target, ProfilingBudget())


def _statements(dialect: str) -> list[str]:
    p = _profiler(dialect)
    t = p.table("sales", "orders")
    cols = COLUMNS[dialect]
    out: list[str] = []
    out.append(p.scan_sql(t, cols)[0])
    exprs = [e for e in (p.sample_expr(n, dt) for n, dt in cols) if e]
    for whole in (True, False):
        out.append(p.sample_sql(t, exprs, 10_000, 2_000_000, 42, whole))
        out.append(p.sample_sql(t, [p.sample_expr("code", cols[1][1])], 10_000, 2_000_000, 42,
                                whole))  # Stage 3 inclusion values
    for name, dt in cols[:2]:
        for data_type in (None, dt):  # with a type, blank text is excluded
            out.append(p.topk_sql(t, name, 1001, data_type))
            out.append(p.uniqueness_sql(t, name, data_type))
            out.append(p.non_null_sql(t, name, data_type))
    out.append(p.topk_batch_sql(t, cols[:2], 1003))
    out.append(p.topk_batch_sql(t, cols[1:2], 1003))
    for name, dt in cols:
        out.append(p.scan_sql(t, [(name, dt)], {name})[0])  # columnstore: one column each
    approx = p.approx_distinct_batch_sql(t, ["id", "code", "amount"])
    if approx:
        out.append(approx)
    for data_type in ("integer", "uuid"):
        sql, _params = p.inclusion_sql(t, "code", ["a", "b", "c"], data_type)
        out.append(sql)
    out.append(p.range_count_sql(t, "id", 1, 1000)[0])
    log: list[str] = []
    cur = _Cursor(log)
    p.modules(cur)
    p.can_select(cur, "sales", "orders")
    p.distinct_estimate(cur, "sales", "orders", "code")
    return out + log


def _placeholders(sql: str, dialect: str) -> str:
    if dialect == "postgres":
        return sql.replace("%s", "'{}'")
    return sql.replace("?", "1")


@pytest.mark.parametrize("dialect", ["postgres", "mssql"])
def test_every_statement_is_a_select_and_never_an_unordered_sample(dialect):
    statements = _statements(dialect)
    assert len(statements) >= 15
    read = "tsql" if dialect == "mssql" else "postgres"
    for sql in statements:
        tree = sqlglot.parse_one(_placeholders(sql, dialect), read=read)
        assert isinstance(tree, exp.Select | exp.Union), sql
        assert not any(isinstance(node, DDL_OR_DML) for node in tree.walk()), sql
        for node in tree.walk():
            if not isinstance(node, exp.Select | exp.Union) or not node.args.get("limit"):
                continue
            ordered = node.args.get("order") is not None
            sampled = any(True for _ in node.find_all(exp.TableSample))
            assert ordered or sampled, f"unordered row cap used as a sample: {sql}"
        if dialect == "mssql" and "FROM [sales].[orders]" in sql:
            assert "OPTION (MAXDOP 2)" in sql, sql


@pytest.mark.parametrize("dialect", ["postgres", "mssql"])
def test_samples_use_tablesample_and_never_a_plain_row_cap(dialect):
    p = _profiler(dialect)
    t = p.table("sales", "orders")
    sampled = p.sample_sql(t, ["a"], 10_000, 2_000_000, 42, False)
    assert "TABLESAMPLE SYSTEM" in sampled and "REPEATABLE (42)" in sampled
    assert "TOP" not in sampled and "LIMIT" not in sampled
    whole = p.sample_sql(t, ["a"], 10_000, 2_000, 42, True)
    assert "TABLESAMPLE" not in whole and "TOP" not in whole and "LIMIT" not in whole


def test_binary_and_lob_columns_are_not_sampled_raw():
    pg, ms = _profiler("postgres"), _profiler("mssql")
    assert pg.sample_expr("raw", "bytea") is None
    assert pg.sample_expr("note", "text") == 'left("note"::text, 200)'
    assert ms.sample_expr("raw", "varbinary(max)") is None
    assert ms.sample_expr("old", "ntext") is None
    assert ms.sample_expr("note", "nvarchar(max)") == "LEFT([note], 200)"
    scan = ms.scan_sql("[s].[t]", [("flag", "bit")])[0]
    assert "MIN(CAST([flag] AS int))" in scan  # MIN/MAX fail on bit (2026-11-13)


def _violations(sql: str, read: str) -> int:
    tree = sqlglot.parse_one(sql, read=read)
    return sum(
        1 for node in tree.walk()
        if isinstance(node, exp.Select | exp.Union) and node.args.get("limit")
        and node.args.get("order") is None and not any(True for _ in node.find_all(
            exp.TableSample))
    )


def test_the_rule_itself_catches_an_unordered_top():
    assert _violations("SELECT TOP (200) * FROM [s].[t]", "tsql") == 1
    assert _violations('SELECT * FROM "s"."t" LIMIT 200', "postgres") == 1
    assert _violations("SELECT TOP (5) a, COUNT_BIG(*) AS n FROM [s].[t] GROUP BY a "
                       "ORDER BY n DESC", "tsql") == 0
    assert _violations("SELECT TOP (5) a FROM [s].[t] TABLESAMPLE SYSTEM (100 ROWS)",
                       "tsql") == 0
