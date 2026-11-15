"""PostgreSQL backend of the Phase 3.0 measurement harness (``tools/measure_target``).

The first run of a statement is a plain execution (value and wall time). The
repeat run is ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``: its buffer counts
give **pages** touched (hit + read, independent of cache state), **physical**
pages (read) and temp pages (sorts/hashes that spilled). Its time is the
server's execution time, i.e. excludes result transfer.

Sessions are autocommit and read-only, so a timeout only cancels its own
statement. Parallelism is capped with ``max_parallel_workers_per_gather``, the
PostgreSQL counterpart of SQL Server's MAXDOP.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from gaia.core.config import TargetConfig
from gaia.dialects.errors import StatementTimeout, TargetError
from gaia.dialects.postgres.connection import GuardedCursor, session
from gaia.dialects.types import base_type

__all__ = ["PostgresMeasure", "buffers_from_plan"]

_COUNT_ONLY = {"json", "xml", "point", "line", "lseg", "box", "path", "polygon", "circle"}
_EQUALITY_ONLY = {"uuid", "jsonb", "bytea", "tsvector"}
_DECIMAL = {"numeric", "decimal", "money"}
_NUMERIC = _DECIMAL | {"smallint", "integer", "int", "bigint", "real", "double precision"}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def buffers_from_plan(plan_json: Any) -> dict[str, Any]:
    """Pages from an ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` result."""
    doc = plan_json[0] if isinstance(plan_json, list) else plan_json
    plan = doc["Plan"]
    hit = int(plan.get("Shared Hit Blocks", 0))
    read = int(plan.get("Shared Read Blocks", 0))
    temp = int(plan.get("Temp Read Blocks", 0)) + int(plan.get("Temp Written Blocks", 0))
    return {
        "pages": hit + read,
        "physical": read,
        "worktable": temp,
        "server_ms": round(float(doc.get("Execution Time", 0.0)), 1),
    }


class PostgresMeasure:
    dialect = "postgres"
    has_approx_distinct = False  # no core APPROX_COUNT_DISTINCT; pg_stats estimate instead
    has_approx_percentile = False

    def __init__(self, target: TargetConfig, *, statement_timeout_s: int, maxdop: int) -> None:
        self.target = target
        self.statement_timeout_s = statement_timeout_s
        self.maxdop = maxdop

    @contextmanager
    def session(self) -> Iterator[GuardedCursor]:
        with session(
            self.target,
            autocommit=True,
            isolation="read_committed",
            statement_timeout_s=self.statement_timeout_s,
        ) as cur:
            cur.execute(f"SET max_parallel_workers_per_gather = {int(self.maxdop)}")
            yield cur

    def run(self, cur: GuardedCursor, sql: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
            return {"ms": round((time.perf_counter() - started) * 1000, 1), "rows": rows}
        except StatementTimeout:
            return {"ms": round((time.perf_counter() - started) * 1000, 1), "timed_out": True}
        except (psycopg.Error, TargetError) as exc:
            return {"ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)}

    def repeat(self, cur: GuardedCursor, sql: str) -> dict[str, Any]:
        result = self.run(cur, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
        if "rows" not in result:
            return result
        plan = result["rows"][0][0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        io = buffers_from_plan(plan)
        return {"ms": io.pop("server_ms"), **io}

    # -- statements ---------------------------------------------------------------------

    def table(self, schema: str, name: str) -> str:
        return f"{quote(schema)}.{quote(name)}"

    def kind(self, data_type: str) -> str:
        raw = data_type.strip()
        if raw.endswith("[]"):
            return "equality_only"
        base = base_type(raw)
        if base in _COUNT_ONLY:
            return "count_only"
        if base in _EQUALITY_ONLY:
            return "equality_only"
        if base in ("boolean", "bool"):
            return "bit"
        if base in _DECIMAL:
            return "decimal"
        if base in _NUMERIC:
            return "numeric"
        return "ordered"

    def count(self, t: str) -> str:
        return f"SELECT count(*) FROM {t}"

    def scan(self, t: str, columns: list[tuple[str, str]]) -> str:
        parts = ["count(*)"]
        for name, data_type in columns:
            c = quote(name)
            kind = self.kind(data_type)
            parts.append(f"count({c})")
            if kind in ("count_only", "equality_only"):
                continue
            expr = f"{c}::int" if kind == "bit" else c
            parts += [f"min({expr})", f"max({expr})"]
        return f"SELECT {', '.join(parts)} FROM {t}"

    def distinct_exact(self, t: str, column: str) -> str:
        return f"SELECT count(DISTINCT {quote(column)}) FROM {t}"

    def distinct_stats(self, schema: str, name: str, column: str) -> str:
        """The planner's estimate from ``pg_stats``: negative = fraction of rows."""
        return (
            "SELECT CASE WHEN s.n_distinct < 0 THEN round(-s.n_distinct * c.reltuples) "
            "ELSE s.n_distinct END "
            "FROM pg_stats s "
            f"JOIN pg_class c ON c.oid = {literal(self.table(schema, name))}::regclass "
            f"WHERE s.schemaname = {literal(schema)} AND s.tablename = {literal(name)} "
            f"AND s.attname = {literal(column)} "
            "ORDER BY s.inherited DESC LIMIT 1"
        )

    def avg_stdev_native(self, t: str, column: str) -> str:
        c = quote(column)
        return f"SELECT avg({c}), stddev_samp({c}) FROM {t}"

    def avg_stdev_float(self, t: str, column: str) -> str:
        c = f"{quote(column)}::float8"
        return f"SELECT avg({c}), stddev_samp({c}) FROM {t}"

    def avg_float(self, t: str, column: str) -> str:
        return f"SELECT avg({quote(column)}::float8) FROM {t}"

    def percentile_exact(self, t: str, column: str) -> str:
        return f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {quote(column)}) FROM {t}"

    @staticmethod
    def _percent(rows: int, estimated_rows: int | None) -> float:
        if not estimated_rows or estimated_rows <= 0:
            return 100.0
        return round(min(100.0, max(0.0001, 100.0 * rows / estimated_rows)), 6)

    def _sample(self, rows: int, est: int | None, seed: int, method: str = "system") -> str:
        return f"TABLESAMPLE {method.upper()} ({self._percent(rows, est)}) REPEATABLE ({int(seed)})"

    def percentile_sample(self, t: str, column: str, rows: int, est: int | None, seed: int) -> str:
        return (
            f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {quote(column)}) "
            f"FROM {t} {self._sample(rows, est, seed)}"
        )

    def sample_methods(self) -> list[str]:
        return ["system", "bernoulli"]

    def sample_count(self, t: str, rows: int, est: int | None, seed: int, method: str) -> str:
        return f"SELECT count(*) FROM {t} {self._sample(rows, est, seed, method)}"

    def sample_fetch(self, t: str, rows: int, est: int | None, seed: int) -> str:
        return f"SELECT * FROM {t} {self._sample(rows, est, seed)}"

    def topk(self, t: str, column: str, k: int = 25) -> str:
        c = quote(column)
        return f"SELECT {c}, count(*) AS n FROM {t} GROUP BY {c} ORDER BY n DESC LIMIT {int(k)}"

    def group_by(self, t: str, column: str) -> str:
        c = quote(column)
        return f"SELECT count(*) FROM (SELECT {c} FROM {t} GROUP BY {c}) AS g"

    # -- database-level inspection -------------------------------------------------------

    def environment(self, cur: GuardedCursor) -> dict[str, Any]:
        env: dict[str, Any] = {}
        for key, sql in {
            "version": "SELECT current_setting('server_version')",
            "database": "SELECT current_database()",
            "server_started_at": "SELECT pg_postmaster_start_time()::text",
            "shared_buffers": "SELECT current_setting('shared_buffers')",
            "track_io_timing": "SELECT current_setting('track_io_timing')",
            "max_parallel_workers_per_gather": "SELECT current_setting('max_parallel_workers_per_gather')",
            "transaction_read_only": "SELECT current_setting('transaction_read_only')",
        }.items():
            result = self.run(cur, sql)
            rows = result.get("rows")
            env[key] = rows[0][0] if rows else result.get("error")
        env["maxdop"] = self.maxdop
        env["isolation"] = "read_committed (autocommit, read-only)"
        return env

    def stats_inventory(self, cur: GuardedCursor) -> dict[str, dict[str, Any]] | None:
        """Column statistics per table. PostgreSQL creates them only by ANALYZE,
        so profiling queries must leave this unchanged."""
        result = self.run(
            cur,
            """
            SELECT schemaname || '.' || tablename, attname, inherited::text
            FROM pg_stats
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            """,
        )
        if "rows" not in result:
            return None
        return {
            f"{r[0]}:{r[1]}:{r[2]}": {
                "table": r[0],
                "name": r[1],
                "leading_column": r[1],
                "auto_created": False,
            }
            for r in result["rows"]
        }

    def sampling_support(self, cur: GuardedCursor, seed: int) -> list[dict[str, Any]]:
        """``TABLESAMPLE`` on a partitioned table, a materialized view and a view."""
        result = self.run(
            cur,
            """
            SELECT DISTINCT ON (c.relkind) c.relkind::text, n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('p', 'm', 'v') AND NOT c.relispartition
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY c.relkind, n.nspname, c.relname
            """,
        )
        labels = {"p": "partitioned table", "m": "materialized view", "v": "view"}
        out: list[dict[str, Any]] = []
        for relkind, schema, name in result.get("rows", []):
            run = self.run(
                cur,
                f"SELECT count(*) FROM {self.table(schema, name)} "
                f"TABLESAMPLE SYSTEM (1) REPEATABLE ({int(seed)})",
            )
            out.append(
                {
                    "object": f"{schema}.{name}",
                    "kind": labels[relkind],
                    "supported": "rows" in run,
                    "rows": run["rows"][0][0] if run.get("rows") else None,
                    "ms": run.get("ms"),
                    "error": run["error"].splitlines()[0] if run.get("error") else None,
                }
            )
        return out

    def stats_leading_columns(self, cur: GuardedCursor, schema: str, name: str) -> set[str] | None:
        return None  # no auto-created statistics in PostgreSQL
