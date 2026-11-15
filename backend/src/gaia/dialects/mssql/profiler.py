"""SQL Server profiler (Phase 3A): the SQL behind ``gaia.profiling.statistics``.

Extends the measurement backend (same hardened autocommit session with the
profiling statement timeout; every statement ends in ``OPTION (MAXDOP n)``).
Only ``SELECT`` is issued. Exact ``COUNT(DISTINCT)`` / ``GROUP BY`` on columns
without statistics make the engine auto-create statistics (D4′): the profiling
run reports them as its footprint.

Not testable without SQL Server: every statement is parsed with ``sqlglot``
(``read="tsql"``) in the unit tests, and the orchestration degrades one metric
per failing statement, recording the error, so a statement SQL Server rejects
shows up in the profile rather than failing the run.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from gaia.core.config import ProfilingBudget, TargetConfig
from gaia.dialects.mssql.connection import GuardedCursor, session
from gaia.dialects.mssql.measure import MssqlMeasure, quote
from gaia.dialects.types import base_type, type_info, type_length
from gaia.introspect.snapshot import TableSnapshot

__all__ = ["MssqlProfiler"]

_NOT_SAMPLED = {
    "text",
    "ntext",
    "image",
    "xml",
    "geography",
    "geometry",
    "hierarchyid",
    "sql_variant",
    "varbinary",
    "binary",
    "timestamp",
    "rowversion",
}


class MssqlProfiler(MssqlMeasure):
    dialect = "mssql"

    def __init__(self, target: TargetConfig, budget: ProfilingBudget) -> None:
        super().__init__(
            target, statement_timeout_s=budget.statement_timeout_s, maxdop=budget.maxdop
        )
        self.budget = budget
        self.queries = 0
        self.isolation = target.limits.isolation

    @contextmanager
    def session(self) -> Iterator[GuardedCursor]:
        """The hardened session without ``STATISTICS IO`` (pages are estimated
        from the catalog; the informational messages would only cost time)."""
        with session(
            self.target, statement_timeout_s=self.budget.statement_timeout_s, autocommit=True
        ) as cur:
            yield cur

    # -- execution ----------------------------------------------------------------------------

    def query(
        self, cur: GuardedCursor, sql: str, params: Any = None, limit: int | None = None
    ) -> list[tuple]:
        self.queries += 1
        if params:
            cur.execute(sql, *params)
        else:
            cur.execute(sql)
        if not cur.description:
            return []
        rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
        return [tuple(r) for r in rows]

    # -- table facts --------------------------------------------------------------------------

    def is_columnstore(self, table: TableSnapshot) -> bool:
        return any("COLUMNSTORE" in (i.type_desc or "").upper() for i in table.indexes)

    def can_select(self, cur: GuardedCursor, schema: str, name: str) -> bool:
        rows = self.query(
            cur, "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'SELECT')", (self.table(schema, name),)
        )
        return bool(rows and rows[0][0] == 1)

    # -- statements ---------------------------------------------------------------------------

    def sample_expr(self, name: str, data_type: str) -> str | None:
        base = base_type(data_type)
        if base in _NOT_SAMPLED:
            return None
        c = quote(name)
        if type_length(data_type) == -1:  # (max) types: the first 200 characters
            return f"LEFT({c}, 200)"
        return c

    def scan_sql(self, t: str, columns: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
        parts, layout = ["COUNT_BIG(*)"], [("*", "rows")]
        for name, data_type in columns:
            c, kind = quote(name), self.kind(data_type)
            parts.append(f"COUNT_BIG({c})")
            layout.append((name, "non_null"))
            if kind not in ("count_only", "equality_only"):
                expr = f"CAST({c} AS int)" if kind == "bit" else c
                parts += [f"MIN({expr})", f"MAX({expr})"]
                layout += [(name, "min"), (name, "max")]
            info = type_info("mssql", data_type)
            if info.is_text and kind != "count_only":
                parts += [f"MIN(LEN({c}))", f"MAX(LEN({c}))", f"AVG(CAST(LEN({c}) AS float))"]
                layout += [(name, "min_len"), (name, "max_len"), (name, "avg_len")]
        return f"SELECT {', '.join(parts)} FROM {t}" + self._opt(), layout

    def sample_sql(
        self, t: str, exprs: list[str], rows: int, estimated: int | None, seed: int, whole: bool
    ) -> str:
        source = t if whole else f"{t} {self._sample(rows, estimated, seed)}"
        return f"SELECT {', '.join(exprs)} FROM {source}" + self._opt()

    def topk_sql(self, t: str, name: str, limit: int) -> str:
        c = quote(name)
        return (
            f"SELECT TOP ({int(limit)}) {c}, COUNT_BIG(*) AS n FROM {t} "
            f"WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY n DESC, {c}" + self._opt()
        )

    def approx_distinct_batch_sql(self, t: str, names: list[str]) -> str | None:
        """One scan for the distinct counts of many columns (HyperLogLog, ~2 % error)."""
        parts = ", ".join(f"APPROX_COUNT_DISTINCT({quote(n)})" for n in names)
        return f"SELECT {parts} FROM {t}" + self._opt()

    def distinct_estimate(
        self, cur: GuardedCursor, schema: str, table: str, column: str
    ) -> int | None:
        return None  # histogram-based estimates need DBCC/permissions; approx is used instead

    def values_sql(
        self,
        t: str,
        name: str,
        limit: int,
        rows: int,
        estimated: int | None,
        seed: int,
        whole: bool,
    ) -> str:
        """Distinct values of one column for an inclusion test (Stage 3), most frequent
        first: an ordered ``TOP`` over the table or a ``TABLESAMPLE`` source."""
        c = quote(name)
        source = t if whole else f"{t} {self._sample(rows, estimated, seed)}"
        return (
            f"SELECT TOP ({int(limit)}) {c}, COUNT_BIG(*) AS n FROM {source} "
            f"WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY n DESC, {c}" + self._opt()
        )

    def inclusion_sql(
        self, parent: str, column: str, values: list, data_type: str | None = None
    ) -> tuple[str, Any]:
        """Which of ``values`` exist in the parent key (at most ``join_test_values``
        parameters, far below SQL Server's 2,100)."""
        c = quote(column)
        marks = ", ".join("?" for _ in values)
        return (
            f"SELECT DISTINCT {c} FROM {parent} WHERE {c} IN ({marks})" + self._opt(),
            tuple(values),
        )

    def uniqueness_sql(self, t: str, name: str) -> str:
        c = quote(name)
        return (
            f"SELECT COUNT_BIG(*) FROM (SELECT {c} AS v FROM {t} WHERE {c} IS NOT NULL "
            f"GROUP BY {c}) AS g" + self._opt()
        )

    def non_null_sql(self, t: str, name: str) -> str:
        return f"SELECT COUNT_BIG({quote(name)}) FROM {t}" + self._opt()

    # -- modules ------------------------------------------------------------------------------

    def modules(self, cur: GuardedCursor) -> list[tuple[str, str, str, str | None]]:
        rows = self.query(
            cur,
            """
            SELECT s.name, o.name,
                   CASE o.type WHEN 'V' THEN 'view' WHEN 'P' THEN 'procedure'
                               WHEN 'TR' THEN 'trigger' ELSE 'function' END,
                   m.definition
            FROM sys.sql_modules m
            JOIN sys.objects o ON o.object_id = m.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')
            ORDER BY s.name, o.name
            """,
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    @staticmethod
    def elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)
