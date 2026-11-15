"""Stage 1 table profiling (``gaia.profiling.statistics``) on an in-memory profiler:
exactness by size class, budgets, degradation per metric, and provenance."""

from __future__ import annotations

import random
from decimal import Decimal

from gaia.core.config import ProfilingBudget
from gaia.introspect.snapshot import ColumnSnapshot, ConstraintSnapshot, TableSnapshot
from gaia.profiling.budget import RunBudget, TableBudget
from gaia.profiling.statistics import join_sample, profile_table

from _fake_profiler import FakeProfiler

COLUMNS = ["id", "code", "qty", "price", "total", "status", "email", "payload", "blob"]
TYPES = ["integer", "varchar(10)", "integer", "numeric(10,2)", "numeric(12,2)", "varchar(2)",
         "varchar(80)", "json", "bytea"]


def _rows(n: int, seed: int = 7) -> list[tuple]:
    rng = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        qty = rng.randint(1, 9)
        price = Decimal(rng.randint(100, 99_999)) / 100
        out.append((i, f"C{rng.randint(1, 900):07d}", qty, price, price * qty,
                    rng.choice(["OK", "OK", "OK", "CN", "RT"]),
                    f"user{i}@example.com" if rng.random() < 0.9 else None,
                    '{"a": 1}', b"\x00"))
    return out


def _table(rows: int, size_bytes: int) -> TableSnapshot:
    return TableSnapshot(
        schema="shop", name="orders", kind="table", estimated_rows=rows, size_bytes=size_bytes,
        columns=[ColumnSnapshot(name=n, ordinal=i, data_type=t, nullable=n != "id")
                 for i, (n, t) in enumerate(zip(COLUMNS, TYPES, strict=True), start=1)],
        constraints=[ConstraintSnapshot("pk", "primary_key", ["id"])],
    )


def _profile(profiler, table, cfg=None, *, pages=10_000_000, seconds=180.0):
    cfg = cfg or ProfilingBudget()
    budget = TableBudget(pages, seconds, RunBudget(50_000_000))
    return profile_table(profiler, object(), table, dialect="postgres", cfg=cfg, budget=budget,
                         seed=42)


def _provenance_complete(result) -> None:
    for column, metrics in result.metrics.items():
        for key, entry in metrics.items():
            assert "method" in entry, (column, key)
            if "skipped" in entry or "error" in entry:
                continue
            for field in ("rows_scanned", "pages", "runtime_ms"):
                assert field in entry, (column, key, field, entry)


def test_small_table_is_exact_and_every_metric_is_labelled():
    rows = _rows(3_000)
    profiler = FakeProfiler({"shop.orders": (COLUMNS, rows)})
    result = _profile(profiler, _table(3_000, 3_000 * 100))
    assert result.status == "profiled" and result.size_class == "small"
    assert result.row_count == 3_000
    code = result.columns["code"]
    assert code.distinct_method == "exact"
    assert code.distinct == len({r[1] for r in rows})
    assert result.columns["id"].distinct_method in ("exact", "constraint")
    status = result.columns["status"]
    assert status.distinct == 3 and status.top_values[0][0] == "OK"
    email = result.columns["email"]
    assert any(d.semantic_type == "email" for d in email.detectors)
    assert email.null_fraction is not None and 0.05 < email.null_fraction < 0.15
    assert "f7" not in result.columns  # sanity: only real columns
    assert any(i.expression == "total = qty × price" for i in result.identities)
    assert "blob" not in result.join_values and "blob" in result.columns
    assert result.metrics["blob"]["non_null"]["method"] == "exact"  # scanned, not sampled
    _provenance_complete(result)


def test_large_table_samples_and_labels_the_sample():
    rows = _rows(40_000)
    profiler = FakeProfiler({"shop.orders": (COLUMNS, rows)}, estimate=39_000)
    cfg = ProfilingBudget(full_scan_max_pages=10)  # every table is "large"
    result = _profile(profiler, _table(40_000, 40_000 * 100), cfg)
    assert result.size_class == "large"
    assert result.row_count == 40_000  # the batched scan is still exact
    sample = result.metrics["*"]["sample"]
    assert sample["method"] == "sampled_pages" and sample["seed"] == 42
    assert 0 < sample["rows_scanned"] < 40_000
    assert result.columns["status"].distinct_method == "exact"  # low cardinality: GROUP BY
    assert result.columns["email"].distinct_method == "estimate"  # planner estimate
    assert result.metrics["payload"]["distinct"]["method"] == "skipped"  # json: no equality
    _provenance_complete(result)


def test_large_table_batches_approximate_distinct_counts_in_one_scan():
    rows = _rows(40_000)
    profiler = FakeProfiler({"shop.orders": (COLUMNS, rows)}, approx=True)
    cfg = ProfilingBudget(full_scan_max_pages=10)
    result = _profile(profiler, _table(40_000, 40_000 * 100), cfg)
    batches = [sql for sql in profiler.log if sql[0] == "approx"]
    assert len(batches) == 1 and {"email", "price", "total"} <= set(batches[0][2])
    assert result.columns["email"].distinct_method == "approximate"
    assert result.metrics["email"]["distinct"]["shared"] is True
    _provenance_complete(result)


def test_huge_table_takes_every_metric_from_the_sample():
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(20_000))})
    cfg = ProfilingBudget(full_scan_max_pages=10, scan_max_pages=20)
    result = _profile(profiler, _table(20_000, 20_000 * 100), cfg)
    assert result.size_class == "huge"
    assert not any(sql[0] == "scan" for sql in profiler.log)
    assert result.status == "partial"
    assert result.columns["qty"].method == "sampled_pages"
    assert any("scan_max_pages" in n for n in result.notes)


def test_a_refused_budget_degrades_metrics_instead_of_failing():
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(20_000))})
    cfg = ProfilingBudget(full_scan_max_pages=10, sample_target_rows=2_000)
    result = _profile(profiler, _table(20_000, 20_000 * 100), cfg, pages=100)
    assert not any(sql[0] == "scan" for sql in profiler.log)
    assert result.metrics["*"]["scan"]["method"] == "skipped"
    assert "page budget" in result.metrics["*"]["scan"]["skipped"]
    assert result.status == "partial"  # the sample still fitted
    assert result.metrics["*"]["sample"]["rows_scanned"] > 0
    assert result.pages <= 100


def test_a_time_budget_used_up_stops_every_query():
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(500))})
    result = _profile(profiler, _table(500, 500 * 100), seconds=0.0)
    assert profiler.log == []
    assert result.status == "failed"
    assert "time budget" in result.metrics["*"]["scan"]["skipped"]


def test_a_timed_out_statement_degrades_one_metric():
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(20_000))}, fail={"topk"},
                            estimate=None)
    cfg = ProfilingBudget(full_scan_max_pages=10)
    result = _profile(profiler, _table(20_000, 20_000 * 100), cfg)
    status = result.metrics["status"]["distinct"]
    assert status["method"] == "sampled" and "StatementTimeout" in status["error"]
    assert result.columns["status"].distinct_method == "sampled"
    assert result.status == "partial"
    assert result.row_count == 20_000  # everything else still ran


def test_join_sample_is_row_representative_and_deterministic():
    values = ["A"] * 900 + [f"X{i}" for i in range(100)]  # one heavy value, many rare ones
    first = join_sample(values, 50, seed=1)
    assert first == join_sample(values, 50, seed=1)
    rows = sum(n for _v, n in first)
    heavy = dict(first).get("A", 0)
    assert len(first) <= 50
    assert 0.75 < heavy / rows < 0.98  # close to the true 90 %, not 1/50


def test_temporal_granularity():
    from datetime import date, datetime, timedelta

    from gaia.profiling.statistics import granularity

    base = datetime(2025, 1, 1)
    assert granularity([base + timedelta(days=i) for i in range(30)]) == "day"  # midnight only
    assert granularity([base + timedelta(minutes=7 * i) for i in range(30)]) == "minute"
    assert granularity([base + timedelta(seconds=61 * i) for i in range(30)]) == "second"
    assert granularity([date(2020 + i // 12, i % 12 + 1, 1) for i in range(30)]) == "month"
    assert granularity([date(2000 + i, 1, 1) for i in range(30)]) == "year"
    assert granularity([base]) is None  # too few values to tell
