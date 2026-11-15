"""An in-memory stand-in for a dialect profiler (``dialects/<d>/profiler.py``).

Statements are tuples instead of SQL: ``gaia.profiling.statistics`` and
``joins`` only pass them back to ``query``, so the orchestration (budgets,
degradation, provenance, exact vs sampled) can be tested without a database.
The SQL itself is tested separately (``test_profiler_sql.py``) and live
(``integration/test_profiling_pg.py``).
"""

from __future__ import annotations

import random
from typing import Any

from gaia.dialects.errors import StatementTimeout


class FakeProfiler:
    dialect = "postgres"
    isolation = "read_committed"

    def __init__(
        self,
        tables: dict[str, tuple[list[str], list[tuple]]],
        *,
        fail: set[str] | None = None,
        approx: bool = False,
        estimate: int | None = None,
        select_denied: set[str] | None = None,
    ) -> None:
        self.tables = tables
        self.fail = fail or set()
        self.approx = approx
        self.estimate = estimate
        self.select_denied = select_denied or set()
        self.queries = 0
        self.log: list[Any] = []

    # -- session ------------------------------------------------------------------------------

    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    def session(self):
        return self._Session()

    # -- facts -------------------------------------------------------------------------------

    def table(self, schema: str, name: str) -> str:
        return f"{schema}.{name}"

    def kind(self, data_type: str) -> str:
        if data_type in ("json", "xml"):
            return "count_only"
        if data_type == "uuid":
            return "equality_only"
        if data_type == "bit":
            return "bit"
        return "orderable"

    def is_columnstore(self, table) -> bool:
        return False

    def can_select(self, cur, schema: str, name: str) -> bool:
        self.queries += 1
        return f"{schema}.{name}" not in self.select_denied

    def sample_expr(self, name: str, data_type: str) -> str | None:
        return None if data_type == "bytea" else name

    def stats_inventory(self, cur):
        return {}

    def modules(self, cur):
        self.queries += 1
        return []

    # -- statements ---------------------------------------------------------------------------

    def scan_sql(self, t, columns):
        layout = [("*", "rows")]
        for name, data_type in columns:
            layout.append((name, "non_null"))
            if self.kind(data_type) not in ("count_only", "equality_only"):
                layout += [(name, "min"), (name, "max")]
            if data_type.startswith(("varchar", "text", "char")):
                layout += [(name, "min_len"), (name, "max_len"), (name, "avg_len")]
        return ("scan", t, tuple(columns)), layout

    def sample_sql(self, t, exprs, rows, estimated, seed, whole):
        return ("sample", t, tuple(exprs), rows, whole, seed)

    def topk_sql(self, t, name, limit):
        return ("topk", t, name, limit)

    def approx_distinct_batch_sql(self, t, names):
        return ("approx", t, tuple(names)) if self.approx else None

    def distinct_estimate(self, cur, schema, table, column):
        return self.estimate

    def uniqueness_sql(self, t, name):
        return ("uniq", t, name)

    def non_null_sql(self, t, name):
        return ("nonnull", t, name)

    def inclusion_sql(self, parent, column, values, data_type=None):
        return ("inc", parent, column), list(values)

    def values_sql(self, t, name, limit, rows, estimated, seed, whole):
        return ("values", t, name, limit)

    # -- execution ---------------------------------------------------------------------------

    def _col(self, t: str, name: str) -> list[Any]:
        cols, rows = self.tables[t]
        i = cols.index(name)
        return [r[i] for r in rows]

    def query(self, cur, sql, params=None, limit=None):
        self.queries += 1
        self.log.append(sql)
        kind = sql[0]
        if kind in self.fail:
            raise StatementTimeout(
                f"canceling statement due to statement timeout ({kind})", dialect="postgres"
            )
        t = sql[1]
        if kind == "scan":
            cols, rows = self.tables[t]
            out: list[Any] = [len(rows)]
            _, layout = self.scan_sql(t, list(sql[2]))
            for name, metric in layout[1:]:
                values = [v for v in self._col(t, name) if v is not None]
                if metric == "non_null":
                    out.append(len(values))
                elif metric in ("min", "max"):
                    out.append((min if metric == "min" else max)(values) if values else None)
                else:
                    lengths = [len(str(v)) for v in values]
                    if not lengths:
                        out.append(None)
                    elif metric == "min_len":
                        out.append(min(lengths))
                    elif metric == "max_len":
                        out.append(max(lengths))
                    else:
                        out.append(sum(lengths) / len(lengths))
            return [tuple(out)]
        if kind == "sample":
            cols, rows = self.tables[t]
            idx = [cols.index(e) for e in sql[2]]
            chosen = rows
            if not sql[4]:  # sampled: a seeded random subset of about `rows` rows
                rng = random.Random(sql[5])
                chosen = [r for r in rows if rng.random() < sql[3] / max(1, len(rows))]
            out = [tuple(r[i] for i in idx) for r in chosen]
            return out[:limit] if limit is not None else out
        if kind == "topk":
            counts: dict[Any, int] = {}
            for v in self._col(t, sql[2]):
                if v is not None:
                    counts[v] = counts.get(v, 0) + 1
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
            return [(str(v), n) for v, n in ordered[: sql[3]]]
        if kind == "approx":
            return [tuple(len({v for v in self._col(t, n) if v is not None}) for n in sql[2])]
        if kind == "uniq":
            return [(len({v for v in self._col(t, sql[2]) if v is not None}),)]
        if kind == "nonnull":
            return [(sum(1 for v in self._col(t, sql[2]) if v is not None),)]
        if kind == "inc":
            present = set(self._col(t, sql[2]))
            return [(v,) for v in dict.fromkeys(params) if v in present]
        if kind == "values":
            counts = {}
            for v in self._col(t, sql[2]):
                if v is not None:
                    counts[v] = counts.get(v, 0) + 1
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
            return ordered[: sql[3]]
        raise AssertionError(f"unexpected statement {sql!r}")
