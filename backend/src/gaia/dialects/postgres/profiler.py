"""PostgreSQL profiler (Phase 3A): the SQL behind ``gaia.profiling.statistics``.

Extends the measurement backend (same hardened, read-only, autocommit session
with ``max_parallel_workers_per_gather``) with the statements profiling needs.
Distinct counts of low-cardinality columns use ``GROUP BY`` (a hash aggregate),
never ``count(DISTINCT)``, which always sorts (3.0 measurements).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from gaia.core.config import ProfilingBudget, TargetConfig
from gaia.dialects.postgres.connection import GuardedCursor
from gaia.dialects.postgres.measure import PostgresMeasure, literal, quote
from gaia.dialects.types import base_type, type_info
from gaia.introspect.snapshot import TableSnapshot

__all__ = ["PostgresProfiler"]

_USER_SCHEMAS = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'
"""
_UNBOUNDED_TEXT = {"text", "varchar", "citext", "json", "jsonb", "xml"}


class PostgresProfiler(PostgresMeasure):
    dialect = "postgres"
    isolation = "read_committed"

    def __init__(self, target: TargetConfig, budget: ProfilingBudget) -> None:
        super().__init__(
            target, statement_timeout_s=budget.statement_timeout_s, maxdop=budget.maxdop
        )
        self.budget = budget
        self.queries = 0

    @contextmanager
    def session(self) -> Iterator[GuardedCursor]:
        with super().session() as cur:
            if self.budget.work_mem_mb:
                cur.execute(f"SET work_mem = '{int(self.budget.work_mem_mb)}MB'")
            yield cur

    # -- execution ----------------------------------------------------------------------------

    def query(
        self, cur: GuardedCursor, sql: str, params: Any = None, limit: int | None = None
    ) -> list[tuple]:
        self.queries += 1
        cur.execute(sql, params)
        if not cur.description:
            return []
        if limit is None:
            return cur.fetchall()
        return cur.fetchmany(limit)

    # -- table facts --------------------------------------------------------------------------

    def is_columnstore(self, table: TableSnapshot) -> bool:
        return False

    def can_select(self, cur: GuardedCursor, schema: str, name: str) -> bool:
        rows = self.query(
            cur,
            "SELECT has_table_privilege(%s::regclass, 'SELECT')",
            (self.table(schema, name),),
        )
        return bool(rows and rows[0][0])

    # -- statements ---------------------------------------------------------------------------

    def sample_expr(self, name: str, data_type: str) -> str | None:
        """How a column is fetched into the sample; None = not sampled."""
        kind = self.kind(data_type)
        c = quote(name)
        if kind == "count_only" and base_type(data_type) not in ("json", "xml"):
            return None
        if base_type(data_type) == "bytea":
            return None
        if data_type.endswith("[]") or base_type(data_type) in _UNBOUNDED_TEXT:
            return f"left({c}::text, 200)"
        info = type_info("postgres", data_type)
        if not (info.is_numeric or info.is_temporal or info.is_text) and kind != "bit":
            return f"{c}::text"  # enums, domains over unknown types, uuid, inet ...
        return c

    def scan_sql(self, t: str, columns: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
        parts, layout = ["count(*)"], [("*", "rows")]
        for name, data_type in columns:
            c, kind = quote(name), self.kind(data_type)
            parts.append(f"count({c})")
            layout.append((name, "non_null"))
            if kind not in ("count_only", "equality_only"):
                expr = f"{c}::int" if kind == "bit" else c
                parts += [f"min({expr})::text", f"max({expr})::text"]
                layout += [(name, "min"), (name, "max")]
            if type_info("postgres", data_type).is_text:
                parts += [f"min(length({c}))", f"max(length({c}))", f"avg(length({c}))::float8"]
                layout += [(name, "min_len"), (name, "max_len"), (name, "avg_len")]
        return f"SELECT {', '.join(parts)} FROM {t}", layout

    def sample_sql(
        self, t: str, exprs: list[str], rows: int, estimated: int | None, seed: int, whole: bool
    ) -> str:
        source = t if whole else f"{t} {self._sample(rows, estimated, seed)}"
        return f"SELECT {', '.join(exprs)} FROM {source}"

    def topk_sql(self, t: str, name: str, limit: int) -> str:
        c = quote(name)
        return (
            f"SELECT {c}::text, count(*) FROM {t} WHERE {c} IS NOT NULL "
            f"GROUP BY {c} ORDER BY count(*) DESC, {c} LIMIT {int(limit)}"
        )

    def approx_distinct_batch_sql(self, t: str, names: list[str]) -> str | None:
        return None  # no APPROX_COUNT_DISTINCT in core PostgreSQL; see distinct_estimate

    def distinct_estimate(
        self, cur: GuardedCursor, schema: str, table: str, column: str
    ) -> int | None:
        rows = self.query(cur, self.distinct_stats(schema, table, column))
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def inclusion_sql(
        self, parent: str, column: str, values: list, data_type: str | None = None
    ) -> tuple[str, Any]:
        """Which of ``values`` exist in the parent key. Typed parameters, so the
        key's index is used (uuid values arrive as text and are cast)."""
        c = quote(column)
        cast = "::uuid[]" if data_type and base_type(data_type) == "uuid" else ""
        return (
            f"SELECT DISTINCT {c} FROM {parent} WHERE {c} = ANY(%s{cast})",
            (list(values),),
        )

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
        """Distinct values of one column for an inclusion test (Stage 3), most frequent first."""
        c = quote(name)
        source = t if whole else f"{t} {self._sample(rows, estimated, seed)}"
        return (
            f"SELECT {c}, count(*) FROM {source} WHERE {c} IS NOT NULL "
            f"GROUP BY {c} ORDER BY count(*) DESC, {c} LIMIT {int(limit)}"
        )

    def uniqueness_sql(self, t: str, name: str) -> str:
        c = quote(name)
        return f"SELECT count(*) FROM (SELECT {c} FROM {t} WHERE {c} IS NOT NULL GROUP BY {c}) g"

    def non_null_sql(self, t: str, name: str) -> str:
        return f"SELECT count({quote(name)}) FROM {t}"

    # -- modules ------------------------------------------------------------------------------

    def modules(self, cur: GuardedCursor) -> list[tuple[str, str, str, str | None]]:
        sql = f"""
            SELECT n.nspname, c.relname,
                   CASE c.relkind WHEN 'v' THEN 'view' ELSE 'materialized_view' END,
                   pg_get_viewdef(c.oid, true)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('v', 'm') AND {_USER_SCHEMAS}
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype = 'e')
            UNION ALL
            SELECT n.nspname,
                   p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
                   CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END,
                   pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE p.prokind IN ('f', 'p') AND {_USER_SCHEMAS}
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
            ORDER BY 1, 2
            """  # noqa: S608 (module constants only)
        return [tuple(r) for r in self.query(cur, sql)]

    # -- cost model ---------------------------------------------------------------------------

    @staticmethod
    def elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def literal(text: str) -> str:
        return literal(text)
