"""Profiling budgets and policies: size classes, window, freshness, catalog sufficiency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.core.config import ProfilingBudget
from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    RelationSnapshot,
    TableSnapshot,
)
from gaia.profiling.budget import (
    RunBudget,
    TableBudget,
    catalog_sufficient,
    freshness,
    in_window,
    size_class,
    table_pages,
)


def _table(rows=1000, size=None, **kw):
    return TableSnapshot(schema="s", name="t", kind="table", estimated_rows=rows,
                         size_bytes=size,
                         columns=kw.pop("columns", [ColumnSnapshot("a", 1, "integer", False)]),
                         **kw)


def test_size_classes():
    cfg = ProfilingBudget(full_scan_max_pages=100, scan_max_pages=1000)
    assert table_pages(_table(size=8192 * 50)) == 50
    assert table_pages(_table(rows=10_000, size=None)) == 100  # ~80 bytes per row guess
    assert size_class(_table(size=8192 * 100), cfg, False) == "small"
    assert size_class(_table(size=8192 * 101), cfg, False) == "large"
    assert size_class(_table(size=8192 * 101), cfg, True) == "columnstore"
    assert size_class(_table(size=8192 * 1001), cfg, False) == "huge"


def test_table_and_run_budgets():
    run = RunBudget(max_pages=1000)
    first = TableBudget(600, 60, run)
    assert first.refusal(600) is None
    first.spend(600)
    assert "per table" in first.refusal(1)
    second = TableBudget(600, 60, run)
    assert "run page budget" in second.refusal(500)
    assert second.refusal(400) is None
    assert "time budget" in TableBudget(10, 0, RunBudget(10)).refusal(1)


def test_window_wraps_midnight():
    at = lambda h, m=0: datetime(2026, 11, 14, h, m)  # noqa: E731
    assert in_window(None, at(12))
    assert in_window("22:00-06:00", at(23)) and in_window("22:00-06:00", at(5, 59))
    assert not in_window("22:00-06:00", at(6)) and not in_window("22:00-06:00", at(12))
    assert in_window("01:00-03:00", at(2)) and not in_window("01:00-03:00", at(3))


def test_freshness_policy():
    table = _table(rows=1000)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    base = dict(profile_structure_hash=table.structure_hash, profile_rows=1000,
                profiled_at=now - timedelta(days=1), status="profiled", table=table,
                row_count_delta_ratio=0.2, max_age_days=30, now=now)
    assert freshness(**base) == (True, "fresh")
    assert freshness(**{**base, "profile_rows": 850})[0]  # 18 % move: still fresh
    stale, why = freshness(**{**base, "profile_rows": 700})
    assert not stale and "row count moved" in why
    assert freshness(**{**base, "profile_structure_hash": "x"}) == (False, "structure changed")
    assert freshness(**{**base, "profiled_at": now - timedelta(days=31)})[1] == (
        "older than 30 days")
    assert freshness(**{**base, "status": "failed"})[1] == "last attempt was failed"
    assert freshness(**{**base, "profiled_at": None})[1] == "no profile yet"


def test_catalog_sufficiency():
    documented = _table(columns=[
        ColumnSnapshot("id", 1, "integer", False),
        ColumnSnapshot("name", 2, "varchar(50)", False, comment="Customer display name"),
        ColumnSnapshot("country_id", 3, "integer", False),
    ], constraints=[ConstraintSnapshot("pk", "primary_key", ["id"])],
        relations=[RelationSnapshot("fk", "s", "t", ["country_id"], "s", "countries", ["id"])])
    assert catalog_sufficient(documented, "postgres")
    bare = _table(columns=[ColumnSnapshot("id", 1, "integer", False),
                           ColumnSnapshot("f2", 2, "varchar(12)", True)],
                  constraints=[ConstraintSnapshot("pk", "primary_key", ["id"])])
    assert not catalog_sufficient(bare, "postgres")


def test_no_statement_starts_that_could_outlive_the_ceiling():
    import time

    budget = TableBudget(10**9, 180, RunBudget(10**9), statement_timeout_s=60,
                         started=time.perf_counter() - 130)
    assert "could run past the 180 s ceiling" in budget.refusal(1)
    fresh = TableBudget(10**9, 180, RunBudget(10**9), statement_timeout_s=60)
    assert fresh.refusal(1) is None


def test_adaptive_time_rule_plans_with_what_statements_took():
    """D50 (owner, 2026-11-15): dw.sales_wide's ~4 s column statements were refused
    at 120 s of 180 because each was planned at its full 60 s timeout."""
    import time

    def at(elapsed, rule):
        return TableBudget(10**9, 180, RunBudget(10**9), statement_timeout_s=60,
                           started=time.perf_counter() - elapsed, time_rule=rule)

    strict, adaptive = at(130, "strict"), at(130, "adaptive")
    for b in (strict, adaptive):
        b.observe(4.0)
    assert "could run past" in strict.refusal(1)
    assert adaptive.refusal(1) is None  # planned 8 s, fits
    late = at(175, "adaptive")
    late.observe(4.0)
    assert "would likely run past" in late.refusal(1)
    first = at(130, "adaptive")  # nothing observed yet: the full timeout
    assert "could run past" in first.refusal(1)
    slow = at(130, "adaptive")
    slow.observe(60.0)  # a timeout was observed: back to the hard rule
    assert "could run past" in slow.refusal(1)
    quick = at(0, "adaptive")
    quick.observe(0.1)
    assert quick.expected_s() == 5.0  # never planned below 5 s
