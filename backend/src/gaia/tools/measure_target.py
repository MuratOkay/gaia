"""Phase 3.0 measurement harness: what does profiling really cost on this target?

    python -m gaia.tools.measure_target --target demo_mssql
    python -m gaia.tools.measure_target --target demo_pg --table sales.orders
    make measure TARGET=demo_mssql ARGS="--table dw.sales_wide"

Re-measures, per dialect, everything the Phase 3 budget rests on (PHASES 3.0):

* exact ``COUNT(DISTINCT)`` on **every** eligible column — keys and non-keys —
  against ``APPROX_COUNT_DISTINCT`` (SQL Server) or the planner estimate
  (PostgreSQL); the 2026-11-13 measurement used only a unique key;
* ``AVG`` + ``STDEV`` native vs on a ``float`` cast — like for like this time;
* exact vs approximate vs sampled median;
* one batched scan (count / min / max) over all columns: tier 1;
* row-sized ``TABLESAMPLE`` samples, with rows returned and pages touched;
* exact top-k;
* auto-created statistics (SQL Server footprint): a controlled test on one
  column, plus a before/after diff of every statistic in the database;
* ``TABLESAMPLE`` support on columnstore (SQL Server) or on partitioned tables,
  materialized views and views (PostgreSQL).

Headline operations run twice: the first run is cold if the server was just
restarted (physical reads > 0 prove it), the repeat is warm. Every result has
milliseconds and **pages** (SQL Server ``STATISTICS IO`` logical reads;
PostgreSQL ``EXPLAIN (BUFFERS)`` hit + read), the unit the budgets will use.

Safety: only ``SELECT``/``SET``/``EXPLAIN``; read-only sessions; MAXDOP (or
``max_parallel_workers_per_gather``); a per-statement timeout; a total time
budget after which remaining measurements are skipped. A failing or timed-out
statement is recorded and the run continues. Sampled rows are counted, never
stored. On SQL Server the exact counts will auto-create statistics — that is one
of the things being measured, and the report lists every one.

For a cold-cache run on SQL Server: restart the server (``docker restart
sql1``), wait until it accepts connections, then run this immediately.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from gaia.core.config import get_settings
from gaia.dialects import bootstrap  # noqa: F401  (registers built-in dialects)
from gaia.dialects.registry import registry
from gaia.introspect.snapshot import Snapshot, TableSnapshot

__all__ = ["choose_columns", "choose_tables", "derive_findings", "main", "render_markdown"]

TOOL_VERSION = 1


# -- selection ------------------------------------------------------------------------------


def choose_tables(snapshot: Snapshot, explicit: list[str], min_rows: int) -> list[TableSnapshot]:
    """Explicit tables, or: the two largest plus the widest table with >= min_rows."""
    tables = [t for t in snapshot.tables if t.kind == "table"]
    if explicit:
        wanted = {name.lower() for name in explicit}
        chosen = [t for t in tables if t.qualified_name.lower() in wanted]
        missing = wanted - {t.qualified_name.lower() for t in chosen}
        if missing:
            raise SystemExit(f"unknown table(s): {', '.join(sorted(missing))}")
        return chosen
    by_rows = sorted(tables, key=lambda t: (-(t.estimated_rows or 0), t.qualified_name))
    chosen = by_rows[:2]
    big = [t for t in tables if (t.estimated_rows or 0) >= min_rows] or by_rows[:3]
    widest = max(big, key=lambda t: (len(t.columns), t.estimated_rows or 0), default=None)
    if widest is not None and widest not in chosen:
        chosen.append(widest)
    return chosen


def _key_columns(table: TableSnapshot) -> set[str]:
    """Columns that are unique **on their own** (single-column PK/unique/unique index).

    A member of a composite key is not unique by itself, so it counts as a
    non-key column here — that is exactly the "non-key, high-cardinality" case
    the 3.0 re-measurement is after.
    """
    keys: set[str] = set()
    for con in table.constraints:
        if con.kind in ("primary_key", "unique") and len(con.columns) == 1:
            keys.update(con.columns)
    for idx in table.indexes:
        key_cols = [c["name"] for c in idx.columns if not c.get("included")]
        if idx.is_unique and not idx.filter_definition and len(key_cols) == 1:
            keys.update(key_cols)
    return keys


def choose_columns(table: TableSnapshot, backend: Any, max_columns: int) -> list[dict[str, Any]]:
    keys = _key_columns(table)
    cols = sorted(table.columns, key=lambda c: c.ordinal)
    chosen = [
        {"name": c.name, "type": c.data_type, "kind": backend.kind(c.data_type),
         "key": c.name in keys}
        for c in cols
    ]
    return chosen[:max_columns]


# -- measuring ------------------------------------------------------------------------------


class Budget:
    def __init__(self, minutes: float) -> None:
        self.deadline = time.monotonic() + minutes * 60

    @property
    def exhausted(self) -> bool:
        return time.monotonic() > self.deadline


def _value(result: dict[str, Any]) -> Any:
    rows = result.get("rows")
    if not rows:
        return None
    value = rows[0][0]
    if isinstance(value, Decimal):
        return float(value)
    return value


def _slim(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "rows"}


def _measure(
    backend: Any, cur: Any, budget: Budget, op: str, sql: str, *, column: str | None = None,
    repeat: bool = False, keep_rows: bool = False,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"op": op, "column": column, "sql": sql}
    if budget.exhausted:
        entry["skipped"] = "time budget exhausted"
        return entry
    first = backend.run(cur, sql)
    entry["value"] = _value(first)
    if keep_rows and first.get("rows") is not None:
        entry["rows_returned"] = len(first["rows"])
    entry["first"] = _slim(first)
    if repeat and "rows" in first and not budget.exhausted:
        entry["repeat"] = _slim(backend.repeat(cur, sql))
    return entry


def measure_table(backend: Any, cur: Any, table: TableSnapshot, opts: argparse.Namespace,
                  budget: Budget) -> dict[str, Any]:
    t = backend.table(table.schema, table.name)
    columns = choose_columns(table, backend, opts.max_columns)
    est = table.estimated_rows
    out: dict[str, Any] = {
        "table": table.qualified_name, "estimated_rows": est, "size_bytes": table.size_bytes,
        "columns_total": len(table.columns), "columns_measured": len(columns),
        "measurements": [], "auto_stats_test": None,
    }
    m = out["measurements"]

    # Controlled auto-stats test first, before the distinct pass creates statistics.
    leading = backend.stats_leading_columns(cur, table.schema, table.name)
    if leading is not None:
        candidate = next(
            (c for c in columns
             if not c["key"] and c["kind"] != "count_only" and c["name"] not in leading),
            None,
        )
        if candidate is None:
            out["auto_stats_test"] = {"result": "every eligible column already has statistics"}
        else:
            run = _measure(backend, cur, budget, "group_by", backend.group_by(t, candidate["name"]),
                           column=candidate["name"])
            after = backend.stats_leading_columns(cur, table.schema, table.name) or set()
            out["auto_stats_test"] = {
                "column": candidate["name"],
                "statistics_created": candidate["name"] in after,
                "ms": run.get("first", {}).get("ms"),
            }

    m.append(_measure(backend, cur, budget, "count", backend.count(t), repeat=True))
    scan_cols = [(c["name"], c["type"]) for c in columns]
    m.append(_measure(backend, cur, budget, "scan", backend.scan(t, scan_cols), repeat=True))

    distinct: list[dict[str, Any]] = []
    for c in columns:
        if c["kind"] == "count_only":
            continue
        exact = _measure(backend, cur, budget, "distinct_exact",
                         backend.distinct_exact(t, c["name"]), column=c["name"])
        if backend.has_approx_distinct:
            approx = _measure(backend, cur, budget, "distinct_approx",
                              backend.distinct_approx(t, c["name"]), column=c["name"])
        else:
            approx = _measure(backend, cur, budget, "distinct_stats",
                              backend.distinct_stats(table.schema, table.name, c["name"]),
                              column=c["name"])
        exact["key"] = approx["key"] = c["key"]
        m += [exact, approx]
        distinct.append({"column": c, "exact": exact, "approx": approx})

    # Headline columns get a warm repeat: the key, the highest-cardinality
    # non-key column and the lowest-cardinality non-key column.
    measured = [d for d in distinct if isinstance(d["exact"].get("value"), int | float)]
    non_key = [d for d in measured if not d["column"]["key"]]
    headline = []
    keyed = [d for d in measured if d["column"]["key"]]
    if keyed:
        headline.append(keyed[0])
    if non_key:
        headline.append(max(non_key, key=lambda d: d["exact"]["value"]))
        low = [d for d in non_key if d["exact"]["value"] and d["exact"]["value"] > 1]
        if low:
            headline.append(min(low, key=lambda d: d["exact"]["value"]))
    for d in {id(h): h for h in headline}.values():
        # The GROUP BY form can hash-aggregate where COUNT(DISTINCT) sorts
        # (PostgreSQL always sorts for it); it counts NULL as one group.
        grouped = _measure(backend, cur, budget, "distinct_grouped",
                           backend.group_by(t, d["column"]["name"]), column=d["column"]["name"])
        grouped["key"] = d["column"]["key"]
        m.append(grouped)
        for key in ("exact", "approx"):
            entry = d[key]
            if entry["op"] == "distinct_stats":
                continue  # a catalog lookup; nothing to repeat
            if "first" in entry and "repeat" not in entry and not budget.exhausted:
                entry["repeat"] = _slim(backend.repeat(cur, entry["sql"]))
            entry["headline"] = True

    decimals = [c for c in columns if c["kind"] == "decimal"][:2] or [
        c for c in columns if c["kind"] == "numeric" and not c["key"]][:1]
    for c in decimals:
        for op, build in (("avg_stdev_native", backend.avg_stdev_native),
                          ("avg_stdev_float", backend.avg_stdev_float),
                          ("avg_float", backend.avg_float)):
            m.append(_measure(backend, cur, budget, op, build(t, c["name"]), column=c["name"],
                              repeat=True))
    if decimals:
        c = decimals[0]["name"]
        m.append(_measure(backend, cur, budget, "percentile_exact", backend.percentile_exact(t, c),
                          column=c, repeat=True))
        if backend.has_approx_percentile:
            m.append(_measure(backend, cur, budget, "percentile_approx",
                              backend.percentile_approx(t, c), column=c, repeat=True))
        m.append(_measure(backend, cur, budget, "percentile_sample",
                          backend.percentile_sample(t, c, opts.sample_rows, est, opts.seed),
                          column=c, repeat=True))

    for method in backend.sample_methods():
        m.append(_measure(backend, cur, budget, f"sample_count_{method}",
                          backend.sample_count(t, opts.sample_rows, est, opts.seed, method),
                          repeat=True))
    fetch = _measure(backend, cur, budget, "sample_fetch",
                     backend.sample_fetch(t, opts.sample_rows, est, opts.seed), keep_rows=True)
    fetch["value"] = None  # never keep sampled data in a report
    m.append(fetch)

    low_card = [d for d in non_key if 1 < (d["exact"]["value"] or 0) <= 1000]
    if low_card:
        c = min(low_card, key=lambda d: d["exact"]["value"])["column"]["name"]
        top = _measure(backend, cur, budget, "topk", backend.topk(t, c), column=c, repeat=True)
        top["value"] = None
        m.append(top)
    return out


# -- findings --------------------------------------------------------------------------------


def _ms(entry: dict[str, Any] | None, run: str = "first") -> float | None:
    if not entry or run not in entry:
        return None
    return entry[run].get("ms") if "ms" in entry[run] and "error" not in entry[run] and not (
        entry[run].get("timed_out")) else None


def _find(measurements: list[dict[str, Any]], op: str, column: str | None = None):
    return next((e for e in measurements if e["op"] == op and
                 (column is None or e.get("column") == column)), None)


def derive_findings(report: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for tr in report["tables"]:
        name, ms = tr["table"], tr["measurements"]
        count, scan = _find(ms, "count"), _find(ms, "scan")
        exact = [e for e in ms if e["op"] == "distinct_exact" and _ms(e) is not None]
        timed_out = [e["column"] for e in ms if e["op"] == "distinct_exact"
                     and e.get("first", {}).get("timed_out")]
        total = sum(filter(None, [_ms(count), _ms(scan)] + [_ms(e) for e in exact]))
        notes.append(
            f"{name}: exact tier 1+2 (count + batched scan + {len(exact)} exact distinct counts) "
            f"= {total / 1000:.1f} s"
            + (f"; {len(timed_out)} distinct count(s) timed out: {', '.join(timed_out)}"
               if timed_out else "")
        )
        non_key = [e for e in exact if not e.get("key")]
        if non_key:
            worst = max(non_key, key=lambda e: _ms(e) or 0)
            notes.append(
                f"{name}: non-key exact COUNT(DISTINCT) median "
                f"{statistics.median(_ms(e) for e in non_key):.0f} ms, worst "
                f"{_ms(worst):.0f} ms on {worst['column']} ({worst.get('value')} distinct)"
            )
        errors, speed = [], []
        for e in exact:
            approx = next((a for a in ms if a["op"] in ("distinct_approx", "distinct_stats")
                           and a.get("column") == e["column"]), None)
            if approx and isinstance(approx.get("value"), int | float) and e.get("value"):
                errors.append(abs(approx["value"] - e["value"]) / e["value"] * 100)
                if _ms(approx) and approx["op"] == "distinct_approx":
                    speed.append(_ms(e) / _ms(approx) if _ms(approx) else None)
        if errors:
            label = "APPROX_COUNT_DISTINCT" if any(
                a["op"] == "distinct_approx" for a in ms) else "pg_stats estimate"
            notes.append(
                f"{name}: {label} error vs exact: median {statistics.median(errors):.1f} %, "
                f"max {max(errors):.1f} %"
                + (f"; exact/approx time ratio median {statistics.median(speed):.2f}"
                   if [s for s in speed if s] else "")
            )
        for g in (e for e in ms if e["op"] == "distinct_grouped" and _ms(e)):
            ex = _find(ms, "distinct_exact", g["column"])
            if ex and _ms(ex):
                notes.append(f"{name}.{g['column']}: COUNT(DISTINCT) {_ms(ex):.0f} ms vs GROUP BY "
                             f"form {_ms(g):.0f} ms ({ex.get('value')} distinct)")
        for col in {e["column"] for e in ms if e["op"] == "avg_stdev_native"}:
            native, flt = _find(ms, "avg_stdev_native", col), _find(ms, "avg_stdev_float", col)
            for run in ("first", "repeat"):
                a, b = _ms(native, run), _ms(flt, run)
                if a and b:
                    notes.append(f"{name}.{col}: AVG+STDEV native {a:.0f} ms vs float cast "
                                 f"{b:.0f} ms ({run}; like for like, {a / b:.1f}x)")
        pe, pa, ps = (_find(ms, "percentile_exact"), _find(ms, "percentile_approx"),
                      _find(ms, "percentile_sample"))
        if pe and _ms(pe):
            parts = [f"exact {_ms(pe):.0f} ms"]
            if pa and _ms(pa):
                parts.append(f"approx {_ms(pa):.0f} ms")
            if ps and _ms(ps):
                parts.append(f"sample {_ms(ps):.0f} ms")
            notes.append(f"{name}: median " + ", ".join(parts))
        for e in ms:
            if e["op"].startswith("sample_count_") and _ms(e) is not None:
                pages = (e.get("repeat") or e.get("first") or {}).get("pages")
                notes.append(f"{name}: {e['op'][13:]} sample of {report['settings']['sample_rows']}"
                             f" rows returned {e.get('value')} rows in {_ms(e):.0f} ms"
                             + (f", {pages} pages" if pages is not None else ""))
        if count and count.get("first", {}).get("physical") is not None:
            phys = count["first"]["physical"]
            notes.append(f"{name}: first COUNT physical reads = {phys} "
                         f"({'cold cache' if phys else 'warm cache: data was already in memory'})")
        if tr.get("auto_stats_test"):
            test = tr["auto_stats_test"]
            if "column" in test:
                notes.append(f"{name}: GROUP BY on {test['column']} "
                             f"{'created' if test['statistics_created'] else 'did not create'}"
                             " a statistics object")
    fp = report.get("footprint") or {}
    if fp.get("new") is not None:
        notes.append(f"footprint: {len(fp['new'])} statistics object(s) appeared during the run"
                     + (f" ({sum(1 for s in fp['new'] if s.get('auto_created'))} auto-created)"
                        if fp["new"] else ""))
    for s in report.get("sampling_support", []):
        if s.get("object"):
            verdict = "supported" if s.get("supported") else f"NOT supported — {s.get('error')}"
            notes.append(f"TABLESAMPLE on {s['kind']} {s['object']}: {verdict}")
    return notes


# -- report ----------------------------------------------------------------------------------


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) < 1e6 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)[:40]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Measurement report — {report['target']} ({report['dialect']})",
        "",
        f"Started {report['started_at']}, {report['wall_s']:.0f} s wall time. "
        f"Tool v{report['tool_version']}.",
        "",
        "## Environment",
        "",
        "| Setting | Value |",
        "|---|---|",
        *[f"| {k} | {_fmt(v)} |" for k, v in report["environment"].items()],
        *[f"| {k} | {_fmt(v)} |" for k, v in report["settings"].items()],
        "",
        "## Findings",
        "",
        *[f"- {n}" for n in report["findings"]],
        "",
    ]
    for tr in report["tables"]:
        lines += [
            f"## {tr['table']}",
            "",
            f"{_fmt(tr['estimated_rows'])} rows (estimate), {_fmt(tr['size_bytes'])} bytes, "
            f"{tr['columns_measured']}/{tr['columns_total']} columns measured.",
            "",
            "| Operation | Column | Key | First ms | Repeat ms | Pages | Physical | Spill | Value "
            "| Note |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in tr["measurements"]:
            first, rep = e.get("first", {}), e.get("repeat", {})
            note = e.get("skipped") or ("timeout" if first.get("timed_out") else "") or (
                (first.get("error") or "").splitlines()[0][:60] if first.get("error") else "")
            if e.get("rows_returned") is not None:
                note = f"{e['rows_returned']} rows fetched"
            lines.append(
                f"| {e['op']} | {e.get('column') or ''} | {'yes' if e.get('key') else ''} | "
                f"{_fmt(first.get('ms'))} | {_fmt(rep.get('ms'))} | "
                f"{_fmt(rep.get('pages', first.get('pages')))} | "
                f"{_fmt(first.get('physical', rep.get('physical')))} | "
                f"{_fmt(rep.get('worktable', first.get('worktable')))} | {_fmt(e.get('value'))} | "
                f"{note} |"
            )
        lines.append("")
    fp = report.get("footprint") or {}
    if fp.get("new"):
        lines += ["## Statistics created during the run", "",
                  "| Table | Statistic | Leading column | Auto-created |", "|---|---|---|---|"]
        lines += [f"| {s['table']} | {s['name']} | {s['leading_column']} | "
                  f"{'yes' if s.get('auto_created') else 'no'} |" for s in fp["new"]]
        lines.append("")
    if report.get("sampling_support"):
        lines += ["## TABLESAMPLE support", "", "| Object | Kind | Supported | Rows | ms | Error |",
                  "|---|---|---|---|---|---|"]
        for s in report["sampling_support"]:
            err = (s.get("error") or "").splitlines()[0][:80] if s.get("error") else ""
            supported = "" if s.get("object") is None else ("yes" if s.get("supported") else "no")
            lines.append(f"| {s.get('object') or ''} | {s['kind']} | {supported} | "
                         f"{_fmt(s.get('rows'))} | {_fmt(s.get('ms'))} | {err} |")
        lines.append("")
    lines += ["Times: SQL Server = client wall time; PostgreSQL repeat = server execution time "
              "(EXPLAIN ANALYZE). Pages are 8 KB. Physical reads on a first run prove a cold "
              "cache."]
    return "\n".join(lines)


# -- CLI ---------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 3.0 profiling cost measurements.")
    p.add_argument("--target", help="target name from config (default: app.default_target)")
    p.add_argument("--table", action="append", default=[], help="schema.table (repeatable)")
    p.add_argument("--min-rows", type=int, default=1_000_000,
                   help="the widest table is chosen among tables with at least this many rows")
    p.add_argument("--max-columns", type=int, default=60)
    p.add_argument("--statement-timeout", type=int, default=300, help="seconds per statement")
    p.add_argument("--maxdop", type=int, default=2)
    p.add_argument("--sample-rows", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-minutes", type=float, default=90.0, help="total time budget")
    p.add_argument("--out", type=Path, help="JSON report path (default: <tmp>/gaia-measure-*.json)")
    args = p.parse_args(argv)

    settings = get_settings()
    label = args.target or settings.app.default_target
    target = settings.target(label)
    dialect = target.dialect.lower()
    if dialect == "mssql":
        from gaia.dialects.mssql.measure import MssqlMeasure as Backend
    elif dialect in ("postgres", "postgresql"):
        from gaia.dialects.postgres.measure import PostgresMeasure as Backend
    else:
        print(f"no measurement backend for dialect {dialect!r}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    snapshot = registry.reader(target.dialect, config=target).read_snapshot(data_source=label)
    tables = choose_tables(snapshot, args.table, args.min_rows)
    backend = Backend(target, statement_timeout_s=args.statement_timeout, maxdop=args.maxdop)
    budget = Budget(args.max_minutes)
    print(f"measuring {', '.join(t.qualified_name for t in tables)} on {label} "
          f"(timeout {args.statement_timeout}s per statement, budget {args.max_minutes:.0f} min)",
          file=sys.stderr)

    report: dict[str, Any] = {
        "tool": "measure_target", "tool_version": TOOL_VERSION, "target": label,
        "dialect": dialect, "started_at": started_at,
        "settings": {"statement_timeout_s": args.statement_timeout, "maxdop": args.maxdop,
                     "sample_rows": args.sample_rows, "seed": args.seed,
                     "max_columns": args.max_columns},
        "tables": [],
    }
    with backend.session() as cur:
        report["environment"] = backend.environment(cur)
        before = backend.stats_inventory(cur)
        for table in tables:
            print(f"  {table.qualified_name} ...", file=sys.stderr)
            report["tables"].append(measure_table(backend, cur, table, args, budget))
        after = backend.stats_inventory(cur)
        report["sampling_support"] = backend.sampling_support(cur, args.seed)
    if before is not None and after is not None:
        report["footprint"] = {
            "before": len(before), "after": len(after),
            "new": [after[k] for k in sorted(set(after) - set(before))],
        }
    report["wall_s"] = round(time.perf_counter() - started, 1)
    report["findings"] = derive_findings(report)

    stamp = started_at[:19].replace(":", "")
    out = args.out or Path(tempfile.gettempdir()) / f"gaia-measure-{label}-{stamp}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown = render_markdown(report)
    out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nreport: {out} and {out.with_suffix('.md')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
