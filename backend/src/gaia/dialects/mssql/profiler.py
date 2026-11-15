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


_BLANKABLE = {"char", "varchar", "nchar", "nvarchar", "sysname"}


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

    @staticmethod
    def blank_is_null(data_type: str) -> bool:
        """Text types whose empty or blank values count as NULL (owner decision
        2026-11-15: legacy systems store "no value" as ''). ``text``/``ntext``
        allow no comparison and keep SQL Server's own semantics. T-SQL ignores
        trailing spaces in ``=``, so ``NULLIF(c, '')`` also nulls '   '."""
        return base_type(data_type) in _BLANKABLE

    def scan_sql(
        self, t: str, columns: list[tuple[str, str]], distinct: set[str] | None = None
    ) -> tuple[str, list[tuple[str, str]]]:
        """Batched aggregates; ``distinct``: columns whose exact distinct count is
        read in the same pass (columnstore, one column per statement)."""
        parts, layout = ["COUNT_BIG(*)"], [("*", "rows")]
        for name, data_type in columns:
            c, kind = quote(name), self.kind(data_type)
            v = f"NULLIF({c}, '')" if self.blank_is_null(data_type) else c
            parts.append(f"COUNT_BIG({v})")
            layout.append((name, "non_null"))
            if v != c:
                parts.append(f"SUM(CASE WHEN {c} = '' THEN 1 ELSE 0 END)")
                layout.append((name, "empty"))
            if kind not in ("count_only", "equality_only"):
                expr = f"CAST({c} AS int)" if kind == "bit" else v
                parts += [f"MIN({expr})", f"MAX({expr})"]
                layout += [(name, "min"), (name, "max")]
            if distinct and name in distinct:
                parts.append(f"COUNT_BIG(DISTINCT {v})")
                layout.append((name, "distinct"))
            info = type_info("mssql", data_type)
            if info.is_text and kind != "count_only":
                parts += [f"MIN(LEN({v}))", f"MAX(LEN({v}))", f"AVG(CAST(LEN({v}) AS float))"]
                layout += [(name, "min_len"), (name, "max_len"), (name, "avg_len")]
        return f"SELECT {', '.join(parts)} FROM {t}" + self._opt(), layout

    def _present(self, c: str, data_type: str | None) -> str:
        """WHERE condition: a value, not NULL and (text) not blank."""
        if data_type and self.blank_is_null(data_type):
            return f"{c} IS NOT NULL AND {c} <> ''"
        return f"{c} IS NOT NULL"

    def sample_sql(
        self, t: str, exprs: list[str], rows: int, estimated: int | None, seed: int, whole: bool
    ) -> str:
        source = t if whole else f"{t} {self._sample(rows, estimated, seed)}"
        return f"SELECT {', '.join(exprs)} FROM {source}" + self._opt()

    def topk_sql(self, t: str, name: str, limit: int, data_type: str | None = None) -> str:
        c = quote(name)
        return (
            f"SELECT TOP ({int(limit)}) {c}, COUNT_BIG(*) AS n FROM {t} "
            f"WHERE {self._present(c, data_type)} GROUP BY {c} ORDER BY n DESC, {c}"
            + self._opt()
        )

    def topk_batch_sql(self, t: str, columns: list[tuple[str, str]], limit: int) -> str:
        """Exact top-k of several columns in one scan: ``GROUPING SETS`` with one
        set per column, ranked per set. Rows: (``GROUPING_ID``, one value per
        column — only the grouped one is set —, count). NULL and blank groups
        are dropped by the caller; no ``TOP``: the cap is a rank."""
        cols = [quote(n) for n, _dt in columns]
        keys = ", ".join(cols)
        sets = ", ".join(f"({c})" for c in cols)
        inner = (
            f"SELECT GROUPING_ID({keys}) AS gid, {keys}, COUNT_BIG(*) AS n FROM {t} "
            f"GROUP BY GROUPING SETS ({sets})"
        )
        return (
            f"SELECT gid, {keys}, n FROM (SELECT gid, {keys}, n, ROW_NUMBER() OVER "
            f"(PARTITION BY gid ORDER BY n DESC) AS rk FROM ({inner}) AS g) AS r "
            f"WHERE rk <= {int(limit)}" + self._opt()
        )

    def approx_distinct_batch_sql(self, t: str, names: list[str]) -> str | None:
        """One scan for the distinct counts of many columns (HyperLogLog, ~2 % error)."""
        parts = ", ".join(f"APPROX_COUNT_DISTINCT({quote(n)})" for n in names)
        return f"SELECT {parts} FROM {t}" + self._opt()

    def distinct_estimate(
        self, cur: GuardedCursor, schema: str, table: str, column: str
    ) -> int | None:
        return None  # histogram-based estimates need DBCC/permissions; approx is used instead

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

    def range_count_sql(self, parent: str, column: str, low: int, high: int) -> tuple[str, Any]:
        """Keys of the parent between two values (an index range count): the key's
        density where a child's values lie (``joins._local_density``)."""
        c = quote(column)
        return (
            f"SELECT COUNT_BIG(*) FROM {parent} WHERE {c} BETWEEN ? AND ?" + self._opt(),
            (int(low), int(high)),
        )

    def uniqueness_sql(self, t: str, name: str, data_type: str | None = None) -> str:
        c = quote(name)
        return (
            f"SELECT COUNT_BIG(*) FROM (SELECT {c} AS v FROM {t} "
            f"WHERE {self._present(c, data_type)} GROUP BY {c}) AS g" + self._opt()
        )

    def non_null_sql(self, t: str, name: str, data_type: str | None = None) -> str:
        c = quote(name)
        v = f"NULLIF({c}, '')" if data_type and self.blank_is_null(data_type) else c
        return f"SELECT COUNT_BIG({v}) FROM {t}" + self._opt()

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
