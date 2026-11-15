"""Profile one table (Phase 3A, Stage 1): few scans, every metric labelled.

Strategy per table (size class from ``profiling.budget``):

1. **Tier 1 — one batched scan** (``small``, ``large``, ``columnstore``): exact
   row count, non-null counts, min/max and text lengths of every column in a
   single pass. ``huge`` tables, a refused budget or a failing statement fall
   back to the sample for these metrics.
2. **Tier 3 — one sample**: the whole table when it is small enough, otherwise
   ``TABLESAMPLE`` sized in rows. Processed in memory — detectors, parse rates,
   examples, identities, join-candidate values — and never stored.
3. **Tier 2 — distinct counts and top values, per column**:
   * the sample *is* the table → exact;
   * a single-column key → distinct = non-null rows (method ``constraint``);
   * few values in the sample → one exact top-k ``GROUP BY`` (distinct count
     and top values in one query);
   * many values → ``APPROX_COUNT_DISTINCT`` (SQL Server), the planner's
     estimate (PostgreSQL), or an extrapolation from the sample.

Every metric records ``method``, ``rows_scanned``, ``pages`` (estimate),
``runtime_ms``, and ``error``/``skipped`` with the reason when it degraded.
Nothing here raises for a single failing statement: the metric degrades, the
table is ``partial``, the run goes on.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from gaia.core.config import ProfilingBudget
from gaia.dialects.errors import TargetError
from gaia.dialects.types import type_info
from gaia.introspect.snapshot import TableSnapshot
from gaia.profiling.budget import TableBudget, size_class, table_pages
from gaia.profiling.detectors import detect
from gaia.profiling.identities import find_identities
from gaia.profiling.summary import ColumnProfileSummary, IdentityRelation

__all__ = ["TableProfileResult", "profile_table", "to_text"]

_INTEGER = {"tinyint", "smallint", "int", "integer", "bigint", "int2", "int4", "int8"}
_NUMBER = re.compile(r"^[+-]?(\d+([.,]\d+)?|[.,]\d+)$")


def to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return None
    return str(value)


@dataclass
class TableProfileResult:
    table: str
    status: str  # profiled | partial | failed
    size_class: str
    row_count: int | None
    columns: dict[str, ColumnProfileSummary] = field(default_factory=dict)
    metrics: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    identities: list[IdentityRelation] = field(default_factory=list)
    # The most frequent sampled values per column with their sample counts, for
    # join discovery (row-weighted inclusion). In memory only, never stored.
    join_values: dict[str, list[tuple[Any, int]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    queries: int = 0
    pages: int = 0
    runtime_ms: int = 0
    seed: int = 0
    sample_rows: int = 0


class _Recorder:
    def __init__(self, result: TableProfileResult) -> None:
        self.result = result

    def __call__(self, column: str, metric: str, method: str, **detail: Any) -> None:
        entry = {"method": method, **{k: v for k, v in detail.items() if v is not None}}
        self.result.metrics.setdefault(column, {})[metric] = entry


def _run(
    profiler, cur, budget: TableBudget, cost: int, sql: str, params=None, limit: int | None = None
) -> tuple[list | None, dict[str, Any]]:
    """Execute within budget. Returns (rows or None, provenance)."""
    refusal = budget.refusal(cost)
    if refusal:
        return None, {"skipped": refusal}
    started = time.perf_counter()
    try:
        rows = profiler.query(cur, sql, params, limit)
    except TargetError as exc:  # StatementTimeout, LockTimeout
        budget.spend(cost)
        return None, {
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "runtime_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # driver errors: the statement was rejected
        return None, {
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "runtime_ms": int((time.perf_counter() - started) * 1000),
        }
    budget.spend(cost)
    return rows, {"pages": cost, "runtime_ms": int((time.perf_counter() - started) * 1000)}


def _key_columns(table: TableSnapshot) -> set[str]:
    keys: set[str] = set()
    for con in table.constraints:
        if con.kind in ("primary_key", "unique") and len(con.columns) == 1:
            keys.update(con.columns)
    for idx in table.indexes:
        cols = [c["name"] for c in idx.columns if not c.get("included")]
        if idx.is_unique and not idx.filter_definition and not idx.is_disabled and len(cols) == 1:
            keys.update(cols)
    return keys


def _parse_rates(values: list[str]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    from gaia.profiling.detectors import _date_int, _date_text, _datetime_text

    numeric = sum(1 for v in values if _NUMBER.match(v.strip())) / len(values)
    dates = sum(1 for v in values if _date_text(v) or _datetime_text(v) or _date_int(v))
    return round(numeric, 4), round(dates / len(values), 4)


def profile_table(
    profiler,
    cur,
    table: TableSnapshot,
    *,
    dialect: str,
    cfg: ProfilingBudget,
    budget: TableBudget,
    seed: int,
) -> TableProfileResult:
    started = time.perf_counter()
    queries_before = profiler.queries
    t = profiler.table(table.schema, table.name)
    columns = [(c.name, c.data_type) for c in sorted(table.columns, key=lambda c: c.ordinal)]
    pages = table_pages(table)
    cls = size_class(table, cfg, profiler.is_columnstore(table))
    result = TableProfileResult(
        table=table.qualified_name, status="profiled", size_class=cls, row_count=None, seed=seed
    )
    record = _Recorder(result)
    keys = _key_columns(table)
    degraded = False

    # -- tier 1: one batched scan ---------------------------------------------------------
    scan: dict[tuple[str, str], Any] = {}
    if cls != "huge":
        sql, layout = profiler.scan_sql(t, columns)
        rows, prov = _run(profiler, cur, budget, pages, sql)
        if rows:
            scan = dict(zip(layout, rows[0], strict=True))
            result.row_count = int(scan[("*", "rows")])
            record("*", "rows", "exact", rows_scanned=result.row_count, **prov)
        else:
            degraded = True
            result.notes.append(f"batched scan not run: {prov.get('skipped') or prov.get('error')}")
            record("*", "scan", "skipped", **prov)
    else:
        result.notes.append(
            f"table has {pages:,} pages (> scan_max_pages): metrics from the sample"
        )

    # -- tier 3: one sample -----------------------------------------------------------------
    sampled = [(n, dt, profiler.sample_expr(n, dt)) for n, dt in columns]
    sampled = [(n, dt, e) for n, dt, e in sampled if e is not None]
    estimated = result.row_count if result.row_count is not None else table.estimated_rows
    whole = (estimated or 0) <= cfg.sample_target_rows
    sample: list[tuple] = []
    if sampled:
        rows_per_page = max(1.0, (estimated or 1) / pages)
        cost = pages if whole else min(pages, math.ceil(cfg.sample_target_rows / rows_per_page) * 2)
        sql = profiler.sample_sql(
            t, [e for _, _, e in sampled], cfg.sample_target_rows, estimated, seed, whole
        )
        rows, prov = _run(profiler, cur, budget, cost, sql, limit=cfg.max_sample_rows)
        if rows is None:
            degraded = True
            result.notes.append(f"sample not taken: {prov.get('skipped') or prov.get('error')}")
        else:
            sample = rows
            whole = whole and len(sample) < cfg.max_sample_rows
        record(
            "*",
            "sample",
            "exact" if whole else "sampled_pages",
            rows_scanned=len(sample),
            seed=None if whole else seed,
            **prov,
        )
    result.sample_rows = len(sample)
    if whole and sample and result.row_count is None:
        result.row_count = len(sample)  # the sample is the table
    values_by_column = {n: [r[i] for r in sample] for i, (n, _dt, _e) in enumerate(sampled)}

    rows_total = result.row_count if result.row_count is not None else table.estimated_rows
    if result.row_count is None:
        record(
            "*",
            "rows",
            "catalog" if table.estimated_rows is not None else "unknown",
            value=table.estimated_rows,
        )

    # High-cardinality columns of a large table: one batched APPROX_COUNT_DISTINCT
    # scan for all of them (SQL Server) instead of one scan per column.
    approx: dict[str, int] = {}
    approx_prov: dict[str, Any] = {}
    if not whole and cls != "small":
        high = []
        for name, data_type in columns:
            present = [v for v in values_by_column.get(name, []) if v is not None]
            texts = [x for x in (to_text(v) for v in present) if x is not None]
            sd, n = len(set(texts)), len(texts)
            low = sd <= cfg.low_cardinality_max and (n < 100 or sd / max(n, 1) < 0.5)
            if (
                texts
                and not low
                and name not in keys
                and (profiler.kind(data_type) != "count_only")
            ):
                high.append(name)
        batch_sql = profiler.approx_distinct_batch_sql(t, high) if high else None
        if batch_sql:
            rows, approx_prov = _run(profiler, cur, budget, pages, batch_sql)
            if rows:
                approx = {n: int(v) for n, v in zip(high, rows[0], strict=True) if v is not None}
            else:
                degraded = True
                result.notes.append(
                    f"approximate distinct counts not run: "
                    f"{approx_prov.get('skipped') or approx_prov.get('error')}"
                )

    numeric_cols, temporal_cols = [], []
    for name, data_type in columns:
        info = type_info(dialect, data_type)
        values = [v for v in values_by_column.get(name, []) if v is not None]
        texts = [x for x in (to_text(v) for v in values) if x is not None]
        summary = ColumnProfileSummary(
            column=name, method="exact" if scan else "sampled_pages", rows=rows_total
        )
        # non-null, min/max, lengths: the scan if there was one, else the sample
        if scan:
            summary.non_null = int(scan[(name, "non_null")])
            summary.minimum = to_text(scan.get((name, "min")))
            summary.maximum = to_text(scan.get((name, "max")))
            if (name, "max_len") in scan:
                summary.min_length = _int(scan[(name, "min_len")])
                summary.max_length = _int(scan[(name, "max_len")])
                avg = scan[(name, "avg_len")]
                summary.avg_length = round(float(avg), 2) if avg is not None else None
            record(name, "non_null", "exact", rows_scanned=result.row_count)
        elif name in values_by_column and sample:
            share = len(values) / len(sample)
            summary.non_null = round(share * rows_total) if rows_total else len(values)
            ordered = _sorted(values)
            if ordered and kind_orderable(profiler, data_type):
                summary.minimum, summary.maximum = to_text(ordered[0]), to_text(ordered[-1])
            if info.is_text and texts:
                lengths = [len(x) for x in texts]
                summary.min_length, summary.max_length = min(lengths), max(lengths)
                summary.avg_length = round(sum(lengths) / len(lengths), 2)
            record(name, "non_null", "sampled_pages", rows_scanned=len(sample))
        else:
            record(name, "non_null", "skipped", skipped="not sampled (binary or LOB type)")

        # distinct and top values
        counts = Counter(texts)
        if whole and name in values_by_column:
            summary.distinct = len(counts)
            summary.distinct_method = "exact"
            summary.top_values = _top(counts, 1.0)
            record(name, "distinct", "exact", rows_scanned=len(sample))
        elif name in keys and summary.non_null is not None:
            summary.distinct = summary.non_null
            summary.distinct_method = "constraint"
            record(name, "distinct", "constraint")
        elif counts and profiler.kind(data_type) == "count_only":
            record(name, "distinct", "skipped", skipped="type has no equality operator")
        elif counts:
            sd, n = len(counts), len(texts)
            low = sd <= cfg.low_cardinality_max and (n < 100 or sd / n < 0.5)
            if low:
                rows, prov = _run(
                    profiler,
                    cur,
                    budget,
                    pages,
                    profiler.topk_sql(t, name, cfg.low_cardinality_max + 1),
                )
                if rows is not None and len(rows) <= cfg.low_cardinality_max:
                    exact = Counter({to_text(v): int(c) for v, c in rows})
                    summary.distinct = len(exact)
                    summary.distinct_method = "exact"
                    summary.top_values = _top(exact, 1.0)
                    record(name, "distinct", "exact", rows_scanned=rows_total, **prov)
                else:
                    if rows is None:
                        degraded = True
                    scale = (summary.non_null or n) / n
                    summary.distinct = sd
                    summary.distinct_method = "sampled"
                    summary.top_values = _top(counts, scale)
                    record(
                        name,
                        "top_values",
                        "sampled_pages",
                        rows_scanned=n,
                        note="counts scaled from the sample",
                    )
                    record(
                        name,
                        "distinct",
                        "sampled",
                        rows_scanned=n,
                        note="lower bound: distinct values seen in the sample",
                        **prov,
                    )
            else:
                estimate, method, prov = None, None, {}
                if cls == "small":  # P3-5: small tables are exact (one GROUP BY)
                    rows, prov = _run(
                        profiler, cur, budget, pages, profiler.uniqueness_sql(t, name)
                    )
                    if rows:
                        estimate, method = int(rows[0][0]), "exact"
                    else:
                        degraded = True
                if estimate is None and name in approx:
                    estimate, method = approx[name], "approximate"
                    prov = {**approx_prov, "shared": True, "source": "batched approximate scan"}
                if estimate is None:
                    try:
                        estimate = profiler.distinct_estimate(cur, table.schema, table.name, name)
                        method = "estimate" if estimate is not None else None
                    except Exception as exc:  # the estimate is optional
                        result.notes.append(f"{name}: distinct estimate failed: {exc}")
                if estimate is None:
                    estimate = round(sd / n * (summary.non_null or n))
                    method = "sampled"
                summary.distinct = min(estimate, summary.non_null or estimate)
                summary.distinct_method = method
                summary.top_values = _top(counts, (summary.non_null or n) / n)
                record(
                    name,
                    "top_values",
                    "sampled_pages",
                    rows_scanned=n,
                    note="counts scaled from the sample",
                )
                record(
                    name,
                    "distinct",
                    method,
                    rows_scanned=rows_total if method in ("approximate", "exact") else n,
                    **prov,
                )

        summary.sample_values = list(dict.fromkeys(texts))[:5]
        kind = "integer" if info.base in _INTEGER else ("text" if info.is_text else None)
        if kind and texts:
            summary.detectors = detect(texts, kind)
            record(
                name,
                "detectors",
                "sampled_pages" if not whole else "exact",
                rows_scanned=len(texts),
            )
        if info.is_text and texts:
            summary.numeric_parse_rate, summary.date_parse_rate = _parse_rates(texts)
        elif kind == "integer" and texts:
            summary.date_parse_rate = _parse_rates(texts)[1]
        result.columns[name] = summary

        if info.is_numeric and name not in keys and data_type.lower() != "bit":
            numeric_cols.append(name)
        if info.is_temporal:
            temporal_cols.append(name)
            summary.granularity = granularity(values)
        if values and (info.is_text or info.base in _INTEGER or info.is_uuid):
            result.join_values[name] = join_sample(values, cfg.join_test_values, seed)

    if sample:
        names = [n for n, _dt, _e in sampled]
        result.identities = find_identities(
            names, sample, numeric=numeric_cols, temporal=temporal_cols
        )
    _complete_provenance(result)
    if degraded or not scan:
        result.status = "partial" if (scan or sample) else "failed"
    result.queries = profiler.queries - queries_before
    result.pages = budget.pages
    result.runtime_ms = int((time.perf_counter() - started) * 1000)
    return result


def _complete_provenance(result: TableProfileResult) -> None:
    """Every metric gets ``pages``, ``runtime_ms`` and ``rows_scanned``: metrics
    read by the shared batched scan or the sample point at that statement
    (``source``, ``shared``); constraint and catalog values cost nothing."""
    table = result.metrics.get("*", {})
    scan, sample = table.get("rows", {}), table.get("sample", {})
    for column, metrics in result.metrics.items():
        if column == "*":
            continue
        for key, entry in metrics.items():
            if "pages" in entry or "skipped" in entry or "error" in entry:
                continue
            method = entry["method"]
            if method in ("constraint", "estimate", "catalog", "unknown"):
                entry.update(
                    source="constraint" if method == "constraint" else "catalog",
                    pages=0,
                    runtime_ms=0,
                )
                entry.setdefault("rows_scanned", 0)
            elif key == "non_null" and method == "exact" and scan:
                entry.update(
                    source="batched scan",
                    shared=True,
                    pages=scan.get("pages", 0),
                    runtime_ms=scan.get("runtime_ms", 0),
                )
            elif sample:
                entry.update(
                    source="sample",
                    shared=True,
                    pages=sample.get("pages", 0),
                    runtime_ms=sample.get("runtime_ms", 0),
                )


def granularity(values: list[Any]) -> str | None:
    """The finest unit the sampled temporal values use: ``year``, ``month``,
    ``day``, ``minute``, ``second`` or ``sub-second`` (e.g. a datetime column
    that is always midnight holds dates; dates always on the 1st are months)."""
    stamps = [v for v in values if isinstance(v, datetime | date)]
    if len(stamps) < 20:
        return None
    times = [v for v in stamps if isinstance(v, datetime)]
    if any(v.microsecond for v in times):
        return "sub-second"
    if any(v.second for v in times):
        return "second"
    if any(v.hour or v.minute for v in times):
        return "minute"
    if any(v.day != 1 for v in stamps):
        return "day"
    if any(v.month != 1 for v in stamps):
        return "month"
    return "year"


def join_sample(values: list[Any], limit: int, seed: int) -> list[tuple[Any, int]]:
    """A row-representative set of at most ``limit`` distinct values with counts.

    Rows are visited in a seeded random order and counted until the next row
    would add value ``limit + 1``: the visited rows are a uniform random subset
    of the sample, so row-weighted rates computed from them are unbiased (the
    most frequent values would over-represent heavy hitters; the first rows
    would follow the physical order).
    """
    order = list(range(len(values)))
    random.Random(seed).shuffle(order)  # noqa: S311 (sampling, not security)
    counts: dict[Any, int] = {}
    for i in order:
        value = values[i]
        if value is None:
            continue
        if value not in counts and len(counts) >= limit:
            break
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def _sorted(values: list[Any]) -> list[Any]:
    try:
        return sorted(values)
    except TypeError:  # mixed or unorderable types
        return sorted(values, key=str)


def kind_orderable(profiler, data_type: str) -> bool:
    return profiler.kind(data_type) not in ("count_only", "equality_only")


def _top(counts: Counter, scale: float) -> list[tuple[str, int]]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return [(value, max(1, round(count * scale))) for value, count in ordered]
