"""Live SQL Server checks for Phase 3.0 (statement timeout, hardened probe).

Opt-in: ``GAIA_TEST_MSSQL=1`` runs them against the configured SQL Server target
(``GAIA_TEST_MSSQL_TARGET``, default ``demo_mssql``) with the read-only login
from ``.env``. Nothing is written: the probe's DDL attempt is denied and rolled
back, and ``WAITFOR`` only waits.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GAIA_TEST_MSSQL") != "1", reason="GAIA_TEST_MSSQL=1 not set"
)


@pytest.fixture(scope="module")
def target():
    from gaia.core.config import get_settings

    return get_settings().target(os.environ.get("GAIA_TEST_MSSQL_TARGET", "demo_mssql"))


def test_statement_timeout_cancels_and_session_survives(target):
    from gaia.dialects.errors import StatementTimeout
    from gaia.dialects.mssql.connection import session

    with session(target, statement_timeout_s=2, autocommit=True) as cur:
        started = time.perf_counter()
        with pytest.raises(StatementTimeout):
            cur.execute("WAITFOR DELAY '00:00:06'")
        elapsed = time.perf_counter() - started
        assert 1.5 <= elapsed < 5.0, elapsed
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_probe_passes_for_the_read_only_login(target):
    from gaia.dialects.mssql.probe import run_checks

    checks = {c.name: c for c in run_checks(target)}
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert len(checks) == 9


def test_catalog_read_is_deterministic_under_the_timeout(target):
    from gaia.dialects.mssql.catalog import MssqlCatalogReader

    first = MssqlCatalogReader(target).read_snapshot()
    second = MssqlCatalogReader(target).read_snapshot()
    assert first.structure_hash == second.structure_hash
    assert len(first.tables) > 0


# -- Phase 3A: the SQL Server profiler, live -----------------------------------------------------


@pytest.fixture(scope="module")
def mssql_snapshot(target):
    from gaia.dialects.mssql.catalog import MssqlCatalogReader

    return MssqlCatalogReader(target).read_snapshot()


def _profile_live(target, table):
    from gaia.core.config import ProfilingBudget
    from gaia.dialects.mssql.profiler import MssqlProfiler
    from gaia.profiling.budget import RunBudget, TableBudget
    from gaia.profiling.statistics import profile_table

    cfg = ProfilingBudget(max_runtime_per_table_s=120)
    profiler = MssqlProfiler(target, cfg)
    statements = []
    query = profiler.query

    def recording(cur, sql, params=None, limit=None):
        statements.append(sql)
        return query(cur, sql, params, limit)

    profiler.query = recording
    with profiler.session() as cur:
        assert profiler.can_select(cur, table.schema, table.name)
        result = profile_table(profiler, cur, table, dialect="mssql", cfg=cfg,
                               budget=TableBudget(cfg.max_pages_per_table, 120,
                                                  RunBudget(cfg.max_pages_per_run),
                                                  statement_timeout_s=cfg.statement_timeout_s,
                                                  time_rule=cfg.time_rule),
                               seed=42)
    return result, statements


def test_profiler_on_the_smallest_table(target, mssql_snapshot):
    tables = [t for t in mssql_snapshot.tables if t.kind == "table" and (t.estimated_rows or 0)]
    table = min(tables, key=lambda t: (t.estimated_rows, t.qualified_name))
    result, statements = _profile_live(target, table)
    errors = {f"{c}.{k}": e["error"] for c, m in result.metrics.items() for k, e in m.items()
              if "error" in e}
    assert not errors, errors  # every generated statement was accepted by SQL Server
    assert result.status == "profiled" and result.row_count is not None
    assert all("OPTION (MAXDOP" in s for s in statements if "FROM [" in s)


def test_profiler_on_a_large_table_records_every_degradation(target, mssql_snapshot):
    """The largest table (columnstore if there is one): TABLESAMPLE on clustered
    columnstore is one of the 3.0 open questions; whatever SQL Server does, the
    profile must complete and say what degraded."""
    tables = [t for t in mssql_snapshot.tables if t.kind == "table"]
    columnstore = [t for t in tables
                   if any("COLUMNSTORE" in (i.type_desc or "").upper() for i in t.indexes)]
    table = max(columnstore or tables, key=lambda t: (t.estimated_rows or 0, t.qualified_name))
    result, _ = _profile_live(target, table)
    assert result.status in ("profiled", "partial"), result.notes
    print(f"\n{table.qualified_name}: {result.status}, class {result.size_class}, "
          f"{result.runtime_ms} ms, {result.queries} queries; notes: {result.notes}")
    sample = result.metrics["*"].get("sample", {})
    print(f"sample: {sample}")


def test_module_definitions_and_view_lineage(target, mssql_snapshot):
    from gaia.core.config import ProfilingBudget
    from gaia.dialects.mssql.profiler import MssqlProfiler
    from gaia.profiling.lineage import parse_view_lineage

    profiler = MssqlProfiler(target, ProfilingBudget())
    with profiler.session() as cur:
        modules = {(s, n): (k, d) for s, n, k, d in profiler.modules(cur)}
    schema: dict = {}
    for t in mssql_snapshot.tables:
        schema.setdefault(t.schema, {})[t.name] = {c.name: c.data_type for c in t.columns}
    counts = {"pass_through": 0, "computed": 0, "unresolved": 0}
    reasons = set()
    for view in (t for t in mssql_snapshot.tables if t.kind == "view"):
        kind, definition = modules.get((view.schema, view.name), (None, None))
        for item in parse_view_lineage(view.qualified_name, [c.name for c in view.columns],
                                       definition, "mssql", schema):
            counts[item.classification] += 1
            if item.detail:
                reasons.add(item.detail[:80])
    print(f"\nmodules: {len(modules)}; view columns: {counts}; unresolved reasons: {reasons}")
    views = [t for t in mssql_snapshot.tables if t.kind == "view"]
    if views:
        # NULL definitions mean the login lacks VIEW DEFINITION (see OWNER_VERIFICATION)
        assert any(modules.get((v.schema, v.name), (None, None))[1] for v in views)
        assert counts["pass_through"] + counts["computed"] > 0


# -- Phase 4A: the T-SQL guard and the executor, live (asynchronous, D52) -----------------------


def _guard(snapshot, row_limit=50):
    from gaia.dialects.mssql.guard import MssqlGuard
    from gaia.query.catalog import GuardCatalog

    return MssqlGuard(GuardCatalog.from_snapshot(snapshot), row_limit=row_limit)


def test_guarded_statements_run_on_sql_server(target, mssql_snapshot):
    """What the guard renders is valid T-SQL: TOP on a plain query, a wrapped set
    operation, a declared-FK join; the row cap holds."""
    from gaia.query.executor import execute

    tables = sorted((t for t in mssql_snapshot.tables if t.kind == "table" and t.columns),
                    key=lambda t: t.estimated_rows or 0)
    small = tables[0]
    guard = _guard(mssql_snapshot)
    # statements built from catalog names are exactly what the guard is for
    plain = guard.check(f"SELECT * FROM [{small.schema}].[{small.name}]")  # noqa: S608
    assert plain.ok and "TOP 50" in plain.sql, plain.errors
    r = execute(target, plain, row_limit=50)
    assert r.ok and len(r.rows) <= 50, r.error

    col = small.columns[0].name
    union = guard.check(f"SELECT [{col}] FROM [{small.schema}].[{small.name}] UNION "  # noqa: S608
                        f"SELECT [{col}] FROM [{small.schema}].[{small.name}]")
    assert union.ok, union.errors
    assert execute(target, union, row_limit=50).ok

    rel = next(r for t in tables for r in t.relations if len(r.from_columns) == 1)
    join = guard.check(
        f"SELECT COUNT_BIG(*) AS n FROM [{rel.from_schema}].[{rel.from_table}] a "  # noqa: S608
        f"JOIN [{rel.to_schema}].[{rel.to_table}] b "
        f"ON a.[{rel.from_columns[0]}] = b.[{rel.to_columns[0]}]")
    assert join.ok and [j.kind for j in join.joins] == ["fk"], join.errors
    r = execute(target, join, row_limit=50, statement_timeout_s=60)
    assert r.ok and r.rows[0][0] >= 0, r.error
    print(f"\n{small.schema}.{small.name}: {len(execute(target, plain, row_limit=50).rows)} rows; "
          f"join {rel.from_table} -> {rel.to_table}: {r.rows[0][0]}")


def test_executor_timeout_and_forced_write_on_sql_server(target):
    """The executor's own guarantees, with statements forced past the guard: the ODBC
    query timeout cancels, and the read-only login refuses DDL (permission error)."""
    from gaia.query.executor import execute
    from gaia.query.validator import GuardResult

    waiting = GuardResult("mssql", "x", sql="WAITFOR DELAY '00:00:06'")
    r = execute(target, waiting, row_limit=10, statement_timeout_s=2)
    assert r.status == "timeout" and r.elapsed_ms < 5000, r
    forced = GuardResult("mssql", "x", sql="CREATE TABLE dbo.gaia_forced (a int)")
    r = execute(target, forced, row_limit=10)
    assert r.status == "error" and ("262" in r.error or "permission" in r.error.lower()), r.error
