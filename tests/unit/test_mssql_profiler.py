"""The SQL Server profiler driven through ``profile_table`` with a scripted cursor.

No server: the cursor answers each T-SQL statement from an in-memory table, so
this proves the wiring (statement shapes, result layout, positional
parameters, bit handling, MAXDOP on every statement). The live run on SQL
Server is an owner step (OWNER_VERIFICATION.md).
"""

from __future__ import annotations

import random
import re

import _fake_pyodbc as fake

fake.install()

from gaia.core.config import ProfilingBudget, TargetConfig  # noqa: E402
from gaia.dialects.mssql.profiler import MssqlProfiler  # noqa: E402
from gaia.introspect.snapshot import (  # noqa: E402
    ColumnSnapshot,
    ConstraintSnapshot,
    TableSnapshot,
)
from gaia.profiling.budget import RunBudget, TableBudget  # noqa: E402
from gaia.profiling.joins import inclusion_test  # noqa: E402
from gaia.profiling.statistics import profile_table  # noqa: E402

COLS = [("id", "int"), ("cust", "nvarchar(12)"), ("active", "bit"), ("amount", "decimal(10,2)")]


def _rows(n=2_000):
    rng = random.Random(5)
    return [(i, f"C{rng.randint(1, 300):06d}", rng.random() < 0.8, rng.randint(1, 9999) / 100)
            for i in range(1, n + 1)]


class ScriptedCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list[tuple[str, tuple]] = []
        self.description = None
        self._result: list[tuple] = []

    def _column(self, name):
        i = [c for c, _ in COLS].index(name)
        return [r[i] for r in self.rows]

    def execute(self, sql, *params):
        self.statements.append((sql, params))
        self.description = [("c",)]
        if sql.startswith("SELECT COUNT_BIG(*), COUNT_BIG("):
            out = [len(self.rows)]
            for expr in sql[len("SELECT COUNT_BIG(*), "):sql.index(" FROM ")].split(", "):
                m = re.match(r"(COUNT_BIG|MIN|MAX|AVG)\((.*)\)$", expr)
                fn, arg = m.group(1), m.group(2)
                name = re.search(r"\[(\w+)\]", arg).group(1)
                values = self._column(name)
                if "CAST([" in arg and "AS int" in arg:
                    values = [int(v) for v in values]
                if "LEN(" in arg:
                    values = [len(str(v)) for v in values]
                out.append({"COUNT_BIG": len, "MIN": min, "MAX": max,
                            "AVG": lambda v: sum(v) / len(v)}[fn](values))
            self._result = [tuple(out)]
        elif " GROUP BY " in sql and sql.startswith("SELECT TOP ("):
            name = re.search(r"SELECT TOP \(\d+\) \[(\w+)\]", sql).group(1)
            counts = {}
            for v in self._column(name):
                counts[v] = counts.get(v, 0) + 1
            self._result = sorted(counts.items(), key=lambda kv: -kv[1])
        elif sql.startswith("SELECT COUNT_BIG(*) FROM (SELECT"):
            name = re.search(r"SELECT \[(\w+)\] AS v", sql).group(1)
            self._result = [(len(set(self._column(name))),)]
        elif sql.startswith("SELECT DISTINCT [cust]"):
            present = set(self._column("cust"))
            self._result = [(v,) for v in params if v in present]
        elif sql.startswith("SELECT "):  # the sample: whole table (small)
            names = re.findall(r"\[(\w+)\]", sql.split(" FROM ")[0])
            idx = [[c for c, _ in COLS].index(n) for n in names]
            self._result = [tuple(r[i] for i in idx) for r in self.rows]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._result

    def fetchmany(self, n):
        return self._result[:n]


def test_profile_table_through_the_sql_server_profiler():
    target = TargetConfig(dialect="mssql", host="h", database="d", username="u", password="p")
    profiler = MssqlProfiler(target, ProfilingBudget())
    table = TableSnapshot(
        schema="dbo", name="trx", kind="table", estimated_rows=2_000, size_bytes=2_000 * 60,
        columns=[ColumnSnapshot(n, i, t, n != "id") for i, (n, t) in enumerate(COLS, start=1)],
        constraints=[ConstraintSnapshot("pk", "primary_key", ["id"])],
    )
    cur = ScriptedCursor(_rows())
    result = profile_table(profiler, cur, table, dialect="mssql", cfg=ProfilingBudget(),
                           budget=TableBudget(10**9, 180, RunBudget(10**9)), seed=42)
    assert result.status == "profiled", result.notes
    assert result.columns["active"].minimum == "0" and result.columns["active"].maximum == "1"
    assert result.columns["cust"].distinct_method == "exact"
    assert result.columns["amount"].non_null == 2_000
    assert all("OPTION (MAXDOP 2)" in sql for sql, _ in cur.statements)
    assert not any("TABLESAMPLE" in sql for sql, _ in cur.statements)  # small: whole table

    values = [(f"C{i:06d}", 3) for i in range(1, 11)] + [("C999999", 1)]
    outcome = inclusion_test(profiler, cur, "dbo.trx", "cust", values, stage=1,
                             origin="candidate")
    sql, params = cur.statements[-1]
    assert sql.count("?") == len(params) == 11  # positional parameters, no literals
    assert outcome.result.metric is not None
