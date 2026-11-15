"""Phase 3.0 measurement harness: parsing, selection, statements, orchestration.

No database: the SQL Server backend runs against a scripted cursor, the
orchestration against a scripted backend. The live PostgreSQL run is in
``tests/integration/test_pg_target_live.py``.
"""

from __future__ import annotations

import argparse

import _fake_pyodbc as fake
import pytest
import sqlglot

fake.install()

from gaia.core.config import TargetConfig  # noqa: E402
from gaia.dialects.errors import StatementTimeout  # noqa: E402
from gaia.dialects.mssql import measure as mmeasure  # noqa: E402
from gaia.dialects.mssql.measure import MssqlMeasure, parse_statistics_io  # noqa: E402
from gaia.dialects.postgres.measure import PostgresMeasure, buffers_from_plan  # noqa: E402
from gaia.introspect.snapshot import (  # noqa: E402
    ColumnSnapshot,
    ConstraintSnapshot,
    Snapshot,
    TableSnapshot,
)
from gaia.tools import measure_target as mt  # noqa: E402

TARGET = TargetConfig(dialect="mssql", host="h", database="d", username="u", password="p")
MSG = "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"


# -- parsing ---------------------------------------------------------------------------------


def test_statistics_io_parsing():
    messages = [
        ("[01000] (3615)", MSG + "Table 'transaction_lines'. Scan count 9, logical reads 180412, "
         "physical reads 3, page server reads 0, read-ahead reads 179902, page server read-ahead "
         "reads 0, lob logical reads 12, lob physical reads 1, lob page server reads 0, "
         "lob read-ahead reads 4, lob page server read-ahead reads 0."),
        ("[01000] (3615)", MSG + "Table 'Worktable'. Scan count 0, logical reads 0, physical "
         "reads 0, read-ahead reads 0, lob logical reads 0, lob physical reads 0, "
         "lob read-ahead reads 0."),
        ("[01000] (3615)", MSG + "Table 'fact_sales'. Segment reads 44, segment skipped 0."),
        ("[01000] (3615)", MSG + "Table 'Workfile'. Scan count 0, logical reads 640, physical "
         "reads 0, read-ahead reads 0."),
    ]
    assert parse_statistics_io(messages) == {
        "pages": 180412 + 12, "physical": 3 + 179902 + 1 + 4, "worktable": 640,
    }
    assert parse_statistics_io([("[01000] (0)", MSG + "Changed database context.")]) is None
    assert parse_statistics_io([]) is None


def test_buffers_from_plan():
    plan = [{"Plan": {"Shared Hit Blocks": 90, "Shared Read Blocks": 10, "Temp Read Blocks": 3,
                      "Temp Written Blocks": 4}, "Execution Time": 12.345}]
    expected = {"pages": 100, "physical": 10, "worktable": 7, "server_ms": 12.3}
    assert buffers_from_plan(plan) == expected


class ScriptedCursor:
    """Delivers STATISTICS IO only on nextset(), the way the ODBC driver does."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.messages: list = []
        self.description = None
        self._sets = 0

    def execute(self, sql):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        self.description = [("c",)]
        self.messages = []
        self._sets = 0
        return self

    def fetchall(self):
        return [(42,)]

    def nextset(self):
        self._sets += 1
        self.description = None
        if self._sets == 1:
            self.messages = [("[01000] (3615)", MSG + "Table 't'. Scan count 1, logical reads "
                              "7, physical reads 2, read-ahead reads 5.")]
            return False
        return False


def test_mssql_run_collects_io_after_the_rows(monkeypatch):
    monkeypatch.setattr(mmeasure, "pyodbc", fake.FakePyodbc(lambda sql: []))
    backend = MssqlMeasure(TARGET, statement_timeout_s=5, maxdop=2)
    result = backend.run(ScriptedCursor("ok"), "SELECT 1")
    assert result["rows"] == [(42,)] and result["pages"] == 7 and result["physical"] == 7
    timed = backend.run(ScriptedCursor(StatementTimeout("t", dialect="mssql")), "SELECT 1")
    assert timed["timed_out"] and "rows" not in timed
    failed = backend.run(ScriptedCursor(fake.sql_error(8117, "Operand data type bit is invalid")),
                         "SELECT MIN(b) FROM t")
    assert "8117" in failed["error"]


# -- statements -------------------------------------------------------------------------------


@pytest.mark.parametrize(("backend_cls", "dialect"), [(MssqlMeasure, "tsql"),
                                                     (PostgresMeasure, "postgres")])
def test_every_statement_parses(backend_cls, dialect):
    b = backend_cls(TARGET, statement_timeout_s=5, maxdop=2)
    t = b.table("dw", "fact sales")
    flag = "bit" if dialect == "tsql" else "boolean"
    statements = [
        b.count(t), b.scan(t, [("a", "int"), ("f", flag), ("x", "xml")]),
        b.distinct_exact(t, "a"), b.avg_stdev_native(t, "a"), b.avg_stdev_float(t, "a"),
        b.avg_float(t, "a"), b.percentile_exact(t, "a"),
        b.percentile_sample(t, "a", 10_000, 1_000_000, 42), b.sample_fetch(t, 10_000, None, 42),
        b.topk(t, "a"), b.group_by(t, "a"),
        *[b.sample_count(t, 10_000, 1_000_000, 42, m) for m in b.sample_methods()],
    ]
    if b.has_approx_distinct:
        statements += [b.distinct_approx(t, "a"), b.percentile_approx(t, "a")]
    else:
        statements.append(b.distinct_stats("dw", "fact sales", "a"))
    for sql in statements:
        sqlglot.parse_one(sql, read=dialect)


def test_mssql_statements_carry_maxdop_and_cast_bit():
    b = MssqlMeasure(TARGET, statement_timeout_s=5, maxdop=3)
    scan = b.scan("[dw].[t]", [("flag", "bit"), ("g", "uniqueidentifier"), ("n", "nvarchar(max)")])
    assert "MIN(CAST([flag] AS int))" in scan
    assert "MIN([g])" not in scan and "MIN([n])" not in scan  # count only
    assert scan.endswith("OPTION (MAXDOP 3)")
    assert "TABLESAMPLE SYSTEM (10000 ROWS) REPEATABLE (42)" in b.sample_count(
        "[dw].[t]", 10_000, None, 42, "system")
    assert b.topk("[dw].[t]", "c").startswith("SELECT TOP (25) [c], COUNT_BIG(*) AS n")


def test_postgres_sample_percent_is_row_sized():
    b = PostgresMeasure(TARGET, statement_timeout_s=5, maxdop=2)
    assert "SYSTEM (1.0)" in b.sample_count('"s"."t"', 10_000, 1_000_000, 42, "system")
    assert "BERNOULLI (100.0)" in b.sample_count('"s"."t"', 10_000, 500, 42, "bernoulli")
    assert b.kind("integer[]") == "equality_only" and b.kind("jsonb") == "equality_only"
    assert b.kind("json") == "count_only" and b.kind("boolean") == "bit"


# -- selection ---------------------------------------------------------------------------------


def _table(name, rows, ncols, pk=("id",), unique=()):
    cols = [ColumnSnapshot(name="id", ordinal=1, data_type="int", nullable=False)]
    cols += [ColumnSnapshot(name=f"c{i}", ordinal=i + 1, data_type="decimal(12,2)", nullable=True)
             for i in range(1, ncols)]
    cons = [ConstraintSnapshot("pk", "primary_key", list(pk))]
    cons += [ConstraintSnapshot(f"uq{i}", "unique", list(u)) for i, u in enumerate(unique)]
    schema, table = name.split(".")
    return TableSnapshot(schema=schema, name=table, kind="table", estimated_rows=rows,
                         columns=cols, constraints=cons)


def _snapshot():
    return Snapshot(data_source="x", dialect="mssql", database="d", server_version="1", tables=[
        _table("dw.fact_sales", 24_600_000, 12), _table("dbo.transaction_lines", 24_500_000, 15),
        _table("dw.sales_wide", 1_200_000, 80), _table("dbo.small_wide", 900, 200),
        _table("dbo.customers", 50_000, 10),
    ])


def test_auto_selection_is_two_largest_plus_widest_big_table():
    chosen = [t.qualified_name for t in mt.choose_tables(_snapshot(), [], 1_000_000)]
    assert chosen == ["dw.fact_sales", "dbo.transaction_lines", "dw.sales_wide"]


def test_explicit_selection_and_unknown_tables():
    assert [t.name for t in mt.choose_tables(_snapshot(), ["DBO.Customers"], 0)] == ["customers"]
    with pytest.raises(SystemExit, match="unknown table"):
        mt.choose_tables(_snapshot(), ["dbo.nope"], 0)


def test_only_single_column_uniqueness_makes_a_key():
    table = _table("s.t", 10, 4, pk=("id", "c1"), unique=[("c2",)])
    cols = {c["name"]: c["key"] for c in mt.choose_columns(table, MssqlMeasure(
        TARGET, statement_timeout_s=5, maxdop=2), 10)}
    assert cols == {"id": False, "c1": False, "c2": True, "c3": False}


# -- orchestration ----------------------------------------------------------------------------


class ScriptedBackend(MssqlMeasure):
    """Answers by operation; records what ran."""

    def __init__(self, distinct=None, timeouts=(), leading=None):
        super().__init__(TARGET, statement_timeout_s=5, maxdop=2)
        self.distinct = distinct or {}
        self.timeouts = set(timeouts)
        self.leading = leading
        self.ran: list[str] = []
        self.repeated: list[str] = []

    def run(self, cur, sql):
        self.ran.append(sql)
        if any(t in sql for t in self.timeouts):
            return {"ms": 5000.0, "timed_out": True}
        for col, value in self.distinct.items():
            if f"DISTINCT [{col}])" in sql or f"APPROX_COUNT_DISTINCT([{col}])" in sql:
                return {"ms": 100.0, "rows": [(value,)], "pages": 10, "physical": 0}
        if "SELECT * FROM" in sql:
            return {"ms": 50.0, "rows": [("secret",)] * 9000, "pages": 30, "physical": 0}
        return {"ms": 10.0, "rows": [(1,)], "pages": 5, "physical": 2}

    def repeat(self, cur, sql):
        self.repeated.append(sql)
        return {"ms": 5.0, "pages": 5, "physical": 0}

    def stats_leading_columns(self, cur, schema, name):
        if self.leading is None:
            return None
        before = set(self.leading)
        if any("GROUP BY [c1]" in s for s in self.ran):
            before.add("c1")
        return before


def _opts(**kw):
    return argparse.Namespace(max_columns=kw.get("max_columns", 60), sample_rows=10_000, seed=42)


def test_measure_table_end_to_end():
    backend = ScriptedBackend(distinct={"id": 1000, "c1": 400, "c2": 3, "c3": 999},
                              leading={"id"})
    table = _table("dw.fact_sales", 1000, 4)
    out = mt.measure_table(backend, None, table, _opts(), mt.Budget(10))
    ops = [e["op"] for e in out["measurements"]]
    assert ops[:2] == ["count", "scan"]
    assert ops.count("distinct_exact") == 4 and ops.count("distinct_approx") == 4
    assert {"avg_stdev_native", "avg_stdev_float", "avg_float", "percentile_exact",
            "percentile_approx", "percentile_sample", "sample_count_system", "sample_fetch",
            "topk"} <= set(ops)
    assert out["auto_stats_test"] == {"column": "c1", "statistics_created": True, "ms": 10.0}

    fetch = next(e for e in out["measurements"] if e["op"] == "sample_fetch")
    assert fetch["rows_returned"] == 9000 and fetch["value"] is None
    assert "secret" not in repr(out)  # sampled data never lands in the report

    headline = {e["column"] for e in out["measurements"]
                if e["op"] == "distinct_exact" and e.get("headline")}
    assert headline == {"id", "c3", "c2"}  # key, highest and lowest non-key cardinality


def test_timeouts_are_recorded_and_the_budget_stops_new_work():
    backend = ScriptedBackend(distinct={"id": 10}, timeouts={"DISTINCT [c2]"})
    out = mt.measure_table(backend, None, _table("s.t", 10, 3), _opts(), mt.Budget(10))
    c2 = next(e for e in out["measurements"] if e["op"] == "distinct_exact" and e["column"] == "c2")
    assert c2["first"]["timed_out"]

    spent = mt.measure_table(ScriptedBackend(), None, _table("s.t", 10, 3), _opts(), mt.Budget(-1))
    assert all(e.get("skipped") for e in spent["measurements"])


def test_findings_and_markdown():
    backend = ScriptedBackend(distinct={"id": 1000, "c1": 400, "c2": 3, "c3": 999})
    table = mt.measure_table(backend, None, _table("dw.t", 1000, 4), _opts(), mt.Budget(10))
    report = {"target": "demo_mssql", "dialect": "mssql", "started_at": "now", "wall_s": 1.0,
              "tool_version": 1, "environment": {"version": "17"},
              "settings": {"sample_rows": 10_000}, "tables": [table],
              "footprint": {"new": [{"table": "dw.t", "name": "_WA_Sys_1", "leading_column": "c1",
                                     "auto_created": True}]},
              "sampling_support": [{"object": "dw.t", "kind": "clustered columnstore",
                                    "supported": True, "rows": 5, "ms": 1.0}]}
    report["findings"] = mt.derive_findings(report)
    text = "\n".join(report["findings"])
    assert "dw.t: exact tier 1+2" in text
    assert "APPROX_COUNT_DISTINCT error vs exact" in text
    assert "1 statistics object(s) appeared" in text
    assert "TABLESAMPLE on clustered columnstore dw.t: supported" in text
    assert "first COUNT physical reads = 2 (cold cache)" in text
    markdown = mt.render_markdown(report)
    assert "| distinct_exact | id | yes |" in markdown and "_WA_Sys_1" in markdown
