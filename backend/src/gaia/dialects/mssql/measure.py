"""SQL Server backend of the Phase 3.0 measurement harness (``tools/measure_target``).

Builds the measurement statements, runs them with ``SET STATISTICS IO ON`` so
every timing comes with **pages** (logical reads) and **physical reads** — the
unit profiling budgets will be expressed in (P3-5) and the evidence of a cold
cache — and inspects the database for the two open questions of 3.0:
auto-created statistics (footprint) and ``TABLESAMPLE`` on columnstore.

Only ``SELECT`` and ``SET`` are issued. Every aggregate carries
``OPTION (MAXDOP n)``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyodbc

from gaia.core.config import TargetConfig
from gaia.dialects.errors import StatementTimeout, TargetError
from gaia.dialects.mssql.connection import GuardedCursor, session
from gaia.dialects.types import base_type, type_length

__all__ = ["MssqlMeasure", "parse_statistics_io"]

_TABLE_IO = re.compile(r"Table '([^']+)'\. (.*)")
_FIELD = re.compile(r"(lob )?(logical|physical|read-ahead) reads (\d+)")
# Types with no ordering, no equality, or LOB storage: COUNT only.
_COUNT_ONLY = {
    "text",
    "ntext",
    "image",
    "xml",
    "geography",
    "geometry",
    "hierarchyid",
    "sql_variant",
    "uniqueidentifier",
    "timestamp",
    "rowversion",
    "varbinary",
    "binary",
}
_DECIMAL = {"decimal", "numeric", "money", "smallmoney"}
_NUMERIC = _DECIMAL | {"tinyint", "smallint", "int", "bigint", "float", "real"}


def parse_statistics_io(messages: list[Any]) -> dict[str, int] | None:
    """Sum ``STATISTICS IO`` lines into pages / physical reads / worktable reads.

    ``pages`` = logical reads incl. LOB (pages touched, cache-independent);
    ``physical`` = physical + read-ahead reads incl. LOB (pages from disk);
    ``worktable`` = logical reads of Worktable/Workfile (sorts and hashes that
    spilled or spooled).
    """
    found = False
    totals = {"pages": 0, "physical": 0, "worktable": 0}
    for message in messages:
        text = message[1] if isinstance(message, tuple | list) and len(message) > 1 else message
        for line in str(text).replace("\\n", "\n").split("\n"):
            m = _TABLE_IO.search(line)
            if not m:
                continue
            found = True
            name, rest = m.group(1), m.group(2)
            for _lob, kind, value in _FIELD.findall(rest):
                n = int(value)
                if kind == "logical":
                    key = "worktable" if name.startswith(("Worktable", "Workfile")) else "pages"
                    totals[key] += n
                else:
                    totals["physical"] += n
    return totals if found else None


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


class MssqlMeasure:
    dialect = "mssql"
    has_approx_distinct = True
    has_approx_percentile = True

    def __init__(self, target: TargetConfig, *, statement_timeout_s: int, maxdop: int) -> None:
        self.target = target
        self.statement_timeout_s = statement_timeout_s
        self.maxdop = maxdop

    # -- session -------------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[GuardedCursor]:
        with session(
            self.target, statement_timeout_s=self.statement_timeout_s, autocommit=True
        ) as cur:
            cur.execute("SET STATISTICS IO ON")
            yield cur

    def run(self, cur: GuardedCursor, sql: str) -> dict[str, Any]:
        """Execute, fetch everything, and collect timing plus STATISTICS IO."""
        started = time.perf_counter()
        try:
            cur.execute(sql)
            messages = list(cur.messages)
            rows = cur.fetchall() if cur.description else []
            # STATISTICS IO arrives after the rows, on the next result "set".
            for _ in range(16):
                try:
                    more = cur.nextset()
                except pyodbc.Error:
                    break
                messages.extend(cur.messages)
                if not more:
                    break
                if cur.description:
                    cur.fetchall()
            ms = round((time.perf_counter() - started) * 1000, 1)
            # pyodbc may keep the previous messages when nextset() finds nothing.
            messages = list(
                dict.fromkeys(tuple(m) if isinstance(m, list | tuple) else m for m in messages)
            )
            io = parse_statistics_io(messages) or {}
            return {"ms": ms, "rows": rows, **io}
        except StatementTimeout:
            return {"ms": round((time.perf_counter() - started) * 1000, 1), "timed_out": True}
        except (pyodbc.Error, TargetError) as exc:
            return {"ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)}

    def repeat(self, cur: GuardedCursor, sql: str) -> dict[str, Any]:
        return self.run(cur, sql)

    # -- statements ------------------------------------------------------------------

    def _opt(self) -> str:
        return f" OPTION (MAXDOP {int(self.maxdop)})"

    def table(self, schema: str, name: str) -> str:
        return f"{quote(schema)}.{quote(name)}"

    def kind(self, data_type: str) -> str:
        base = base_type(data_type)
        if base in _COUNT_ONLY or type_length(data_type) == -1:
            return "count_only"
        if base == "bit":
            return "bit"
        if base in _DECIMAL:
            return "decimal"
        if base in _NUMERIC:
            return "numeric"
        return "ordered"

    def count(self, t: str) -> str:
        return f"SELECT COUNT_BIG(*) FROM {t}" + self._opt()

    def scan(self, t: str, columns: list[tuple[str, str]]) -> str:
        parts = ["COUNT_BIG(*)"]
        for name, data_type in columns:
            c = quote(name)
            kind = self.kind(data_type)
            parts.append(f"COUNT_BIG({c})")
            if kind == "count_only":
                continue
            expr = f"CAST({c} AS int)" if kind == "bit" else c
            parts += [f"MIN({expr})", f"MAX({expr})"]
        return f"SELECT {', '.join(parts)} FROM {t}" + self._opt()

    def distinct_exact(self, t: str, column: str) -> str:
        return f"SELECT COUNT_BIG(DISTINCT {quote(column)}) FROM {t}" + self._opt()

    def distinct_approx(self, t: str, column: str) -> str:
        return f"SELECT APPROX_COUNT_DISTINCT({quote(column)}) FROM {t}" + self._opt()

    def avg_stdev_native(self, t: str, column: str) -> str:
        c = quote(column)
        return f"SELECT AVG({c}), STDEV({c}) FROM {t}" + self._opt()

    def avg_stdev_float(self, t: str, column: str) -> str:
        c = f"CAST({quote(column)} AS float)"
        return f"SELECT AVG({c}), STDEV({c}) FROM {t}" + self._opt()

    def avg_float(self, t: str, column: str) -> str:
        return f"SELECT AVG(CAST({quote(column)} AS float)) FROM {t}" + self._opt()

    def percentile_exact(self, t: str, column: str) -> str:
        c = quote(column)
        return (
            f"SELECT DISTINCT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {c}) OVER () FROM {t}"
            + self._opt()
        )

    def percentile_approx(self, t: str, column: str) -> str:
        c = quote(column)
        return (
            f"SELECT APPROX_PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {c}) FROM {t}" + self._opt()
        )

    def _sample(self, rows: int, estimated_rows: int | None, seed: int) -> str:
        return f"TABLESAMPLE SYSTEM ({int(rows)} ROWS) REPEATABLE ({int(seed)})"

    def percentile_sample(self, t: str, column: str, rows: int, est: int | None, seed: int) -> str:
        c = quote(column)
        return (
            f"SELECT DISTINCT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {c}) OVER () "
            f"FROM {t} {self._sample(rows, est, seed)}" + self._opt()
        )

    def sample_methods(self) -> list[str]:
        return ["system"]

    def sample_count(self, t: str, rows: int, est: int | None, seed: int, method: str) -> str:
        return f"SELECT COUNT_BIG(*) FROM {t} {self._sample(rows, est, seed)}" + self._opt()

    def sample_fetch(self, t: str, rows: int, est: int | None, seed: int) -> str:
        return f"SELECT * FROM {t} {self._sample(rows, est, seed)}" + self._opt()

    def topk(self, t: str, column: str, k: int = 25) -> str:
        c = quote(column)
        return (
            f"SELECT TOP ({int(k)}) {c}, COUNT_BIG(*) AS n FROM {t} "
            f"GROUP BY {c} ORDER BY n DESC" + self._opt()
        )

    def group_by(self, t: str, column: str) -> str:
        c = quote(column)
        return (
            f"SELECT COUNT_BIG(*) FROM (SELECT {c} AS v FROM {t} GROUP BY {c}) AS g" + self._opt()
        )

    # -- database-level inspection ------------------------------------------------------

    def environment(self, cur: GuardedCursor) -> dict[str, Any]:
        env: dict[str, Any] = {}
        queries = {
            "version": "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50))",
            "edition": "SELECT CAST(SERVERPROPERTY('Edition') AS varchar(200))",
            "database": "SELECT DB_NAME()",
        }
        for key, sql in queries.items():
            result = self.run(cur, sql)
            env[key] = _first(result)
        result = self.run(
            cur,
            "SELECT compatibility_level, is_auto_create_stats_on, is_auto_update_stats_on, "
            "is_auto_update_stats_async_on, is_read_committed_snapshot_on, "
            "snapshot_isolation_state_desc FROM sys.databases WHERE name = DB_NAME()",
        )
        if result.get("rows"):
            row = result["rows"][0]
            env.update(
                compatibility_level=row[0],
                auto_create_stats=bool(row[1]),
                auto_update_stats=bool(row[2]),
                auto_update_stats_async=bool(row[3]),
                read_committed_snapshot=bool(row[4]),
                snapshot_isolation=row[5],
            )
        started = self.run(cur, "SELECT sqlserver_start_time FROM sys.dm_os_sys_info")
        env["server_started_at"] = (
            _first(started)
            if "rows" in started
            else ("unavailable (needs VIEW SERVER PERFORMANCE STATE)")
        )
        env["maxdop"] = self.maxdop
        env["isolation"] = self.target.limits.isolation
        return env

    def stats_inventory(self, cur: GuardedCursor) -> dict[str, dict[str, Any]] | None:
        result = self.run(
            cur,
            """
            SELECT OBJECT_SCHEMA_NAME(s.object_id) + '.' + OBJECT_NAME(s.object_id),
                   s.name, s.auto_created, s.user_created, c.name,
                   CONVERT(varchar(33), STATS_DATE(s.object_id, s.stats_id), 126)
            FROM sys.stats s
            JOIN sys.objects o ON o.object_id = s.object_id
            JOIN sys.stats_columns sc
                 ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
                AND sc.stats_column_id = 1
            JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
            WHERE o.is_ms_shipped = 0 AND o.type = 'U'
            """,
        )
        if "rows" not in result:
            return None
        return {
            f"{r[0]}:{r[1]}": {
                "table": r[0],
                "name": r[1],
                "auto_created": bool(r[2]),
                "user_created": bool(r[3]),
                "leading_column": r[4],
                "updated": r[5],
            }
            for r in result["rows"]
        }

    def sampling_support(self, cur: GuardedCursor, seed: int) -> list[dict[str, Any]]:
        """``TABLESAMPLE`` on every table with a columnstore index (3.0 open question)."""
        result = self.run(
            cur,
            """
            SELECT OBJECT_SCHEMA_NAME(i.object_id), OBJECT_NAME(i.object_id),
                   MAX(CASE WHEN i.type = 5 THEN 1 ELSE 0 END)
            FROM sys.indexes i JOIN sys.objects o ON o.object_id = i.object_id
            WHERE i.type IN (5, 6) AND o.is_ms_shipped = 0 AND o.type = 'U'
            GROUP BY i.object_id
            """,
        )
        out: list[dict[str, Any]] = []
        for schema, name, clustered in result.get("rows", []):
            t = self.table(schema, name)
            run = self.run(
                cur,
                f"SELECT COUNT_BIG(*) FROM {t} TABLESAMPLE SYSTEM (1 PERCENT) "
                f"REPEATABLE ({int(seed)})" + self._opt(),
            )
            out.append(
                {
                    "object": f"{schema}.{name}",
                    "kind": "clustered columnstore" if clustered else "nonclustered columnstore",
                    "supported": "rows" in run,
                    "rows": _first(run),
                    "ms": run.get("ms"),
                    "pages": run.get("pages"),
                    "error": run.get("error"),
                }
            )
        if not out:
            out.append({"object": None, "kind": "no columnstore index in this database"})
        return out

    def stats_leading_columns(self, cur: GuardedCursor, schema: str, name: str) -> set[str] | None:
        result = self.run(
            cur,
            f"""
            SELECT c.name FROM sys.stats s
            JOIN sys.stats_columns sc
                 ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
                AND sc.stats_column_id = 1
            JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
            WHERE s.object_id = OBJECT_ID(N'{self.table(schema, name).replace("'", "''")}')
            """,  # noqa: S608 (quoted identifier, escaped)
        )
        return {r[0] for r in result["rows"]} if "rows" in result else None


def _first(result: dict[str, Any]) -> Any:
    rows = result.get("rows")
    return rows[0][0] if rows else None
