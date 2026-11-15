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
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(20_000))},
                            fail={"topk", "topk_batch"}, estimate=None)
    cfg = ProfilingBudget(full_scan_max_pages=10)
    result = _profile(profiler, _table(20_000, 20_000 * 100), cfg)
    status = result.metrics["status"]["distinct"]
    assert status["method"] == "sampled" and "StatementTimeout" in status["error"]
    assert result.columns["status"].distinct_method == "sampled"
    assert result.status == "partial"
    assert result.row_count == 20_000  # everything else still ran
    # the batch failed first, then each column on its own; the CLI says so (D5)
    assert any("batched top-k not run" in n for n in result.notes)
    assert any(n.startswith("distinct of ") and "StatementTimeout" in n for n in result.notes)


def test_low_cardinality_columns_share_one_grouping_sets_scan():
    """Owner run 2026-11-15: one top-k scan per low-cardinality column ran into
    the per-table page budget after 2-4 columns on SQL Server's large tables."""
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(40_000))})
    cfg = ProfilingBudget(full_scan_max_pages=10)
    result = _profile(profiler, _table(40_000, 40_000 * 100), cfg)
    batches = [sql for sql in profiler.log if sql[0] == "topk_batch"]
    assert len(batches) == 1 and {"qty", "status"} <= set(batches[0][2])
    assert not any(sql[0] == "topk" for sql in profiler.log)
    assert result.columns["status"].distinct == 3
    assert result.columns["status"].distinct_method == "exact"
    assert result.metrics["status"]["distinct"]["source"] == "batched top-k scan"
    _provenance_complete(result)


def test_columnstore_scans_column_by_column_and_counts_exactly():
    """3.0 measurement: the 28-column scan of dw.sales_wide took 60 s (= the
    statement timeout); single-column aggregates took 0.1-2 s, and an exact
    distinct count was cheaper than APPROX_COUNT_DISTINCT."""

    class Columnstore(FakeProfiler):
        def is_columnstore(self, table):
            return True

    profiler = Columnstore({"shop.orders": (COLUMNS, _rows(40_000))}, approx=True,
                           fail=set())
    cfg = ProfilingBudget(full_scan_max_pages=10)
    result = _profile(profiler, _table(40_000, 40_000 * 100), cfg)
    assert result.size_class == "columnstore"
    scans = [sql for sql in profiler.log if sql[0] == "scan"]
    assert len(scans) == len(COLUMNS) and all(len(s[2]) == 1 for s in scans)
    assert not any(sql[0] in ("approx", "uniq") for sql in profiler.log)  # counted in the scan
    assert result.columns["email"].distinct_method == "exact"
    assert result.columns["email"].distinct == len({r[6] for r in _rows(40_000) if r[6]})
    assert result.row_count == 40_000
    assert result.pages < 40_000 * 100 // 8192 * 4  # per-column estimates, not the table each


def test_the_sample_runs_first_so_a_slow_scan_cannot_starve_it():
    """Owner run 2026-11-15: the batched scan of dw.sales_wide timed out at 60 s and
    the 0.2 s sample was then refused by the 120 s ceiling -> `failed`."""
    profiler = FakeProfiler({"shop.orders": (COLUMNS, _rows(20_000))}, fail={"scan"})
    cfg = ProfilingBudget(full_scan_max_pages=10, statement_timeout_s=60)
    budget = TableBudget(10_000_000, 120.0, RunBudget(50_000_000), statement_timeout_s=60)
    result = profile_table(profiler, object(), _table(20_000, 20_000 * 100), dialect="postgres",
                           cfg=cfg, budget=budget, seed=42)
    assert profiler.log[0][0] == "sample"
    assert result.status == "partial" and result.sample_rows > 0
    assert result.columns["qty"].method == "sampled_pages"


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


def test_blank_text_is_null():
    """Owner decision 2026-11-15: '' and '   ' are NULL. Legacy tb_mst01.c07 (a
    credit limit stored as text, 10 % blank) parsed as 88 % numeric and was
    contradicted as a measure."""
    rng = random.Random(3)  # noqa: S311 (test data)
    cols = ["id", "limit_text"]
    rows = [(i, rng.choice(["", "  ", None]) if i % 10 == 0 else f"{rng.randint(1, 50)}000.00")
            for i in range(1, 2_001)]
    table = TableSnapshot(
        schema="shop", name="orders", kind="table", estimated_rows=2_000, size_bytes=2_000 * 40,
        columns=[ColumnSnapshot("id", 1, "integer", False),
                 ColumnSnapshot("limit_text", 2, "varchar(20)", True)],
        constraints=[ConstraintSnapshot("pk", "primary_key", ["id"])],
    )
    for cfg in (ProfilingBudget(), ProfilingBudget(full_scan_max_pages=1, sample_target_rows=500)):
        result = _profile(FakeProfiler({"shop.orders": (cols, rows)}), table, cfg)
        c = result.columns["limit_text"]
        assert c.numeric_parse_rate == 1.0, cfg  # blanks no longer count against numbers
        assert 0.08 < c.null_fraction < 0.12 and c.empty
        assert c.minimum != "" and (c.min_length or 0) >= 7
        assert all(v.strip() for v, _n in c.top_values)
