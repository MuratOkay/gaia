"""Phase 4A on ``demo_pg``: the guard against the real catalog, the executor on the
real database, and ``chat.query_run``.

Needs ``GAIA_TEST_PG_TARGET_URL`` (the read-only role on a seeded ``demo_pg``) and
``GAIA_TEST_DATABASE_URL`` (the disposable metadata store).

What must hold: every statement of a realistic suite is accepted *and runs* on
PostgreSQL (so the SQL the guard renders is valid SQL, not only valid to the
parser); join paths come from FKs, view lineage and model hints only when their
claim test passed (D48) — a ``refuted`` hint or a Stage 1 candidate alone is not
a join path; the statement timeout and the row cap hold; a statement forced past
the guard still meets the read-only session; every run pins its snapshot and
the semantic versions it read.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from gaia.db.models.chat import QueryRun
from gaia.db.models.semantics import JoinHint
from gaia.db.repositories.query_store import QueryStore
from gaia.dialects.postgres.guard import PostgresGuard
from gaia.profiling.service import ProfileOptions, run_profiling
from gaia.query.catalog import load_guard_catalog
from gaia.query.executor import execute
from gaia.query.validator import GuardResult
from gaia.semantics.service import ReasoningOptions, run_reasoning

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")

SUITE = [
    # revenue by store city and month (FK join, date function, enum comparison)
    "SELECT s.city, date_trunc('month', o.order_date) AS month, sum(o.total_amount) AS revenue "
    "FROM sales.orders o JOIN store.stores s ON s.store_id = o.store_id "
    "WHERE o.status <> 'cancelled' GROUP BY 1, 2 ORDER BY 3 DESC",
    # top products by quantity (two FK hops)
    "SELECT p.sku, c.name AS category, sum(l.quantity) AS units FROM sales.order_lines l "
    "JOIN product.products p ON p.product_id = l.product_id "
    "JOIN product.categories c ON c.category_id = p.category_id "
    "GROUP BY p.sku, c.name ORDER BY units DESC LIMIT 10",
    # paid vs ordered, through a CTE; siblings of one key
    "WITH paid AS (SELECT order_id, sum(amount) AS paid FROM sales.payments GROUP BY order_id) "
    "SELECT count(*) FILTER (WHERE paid.paid < o.total_amount) AS underpaid, count(*) AS orders "
    "FROM sales.orders o JOIN paid ON paid.order_id = o.order_id",
    # a window function and a subquery in FROM
    "SELECT customer_id, order_id, total_amount FROM (SELECT customer_id, order_id, total_amount, "
    "row_number() OVER (PARTITION BY customer_id ORDER BY total_amount DESC) AS rn "
    "FROM sales.orders) x WHERE rn = 1",
    # a date spine (generate_series) left-joined to a view
    "SELECT d.day::date AS day, coalesce(sum(v.revenue), 0) AS revenue "
    "FROM generate_series(DATE '2024-01-01', DATE '2024-01-31', INTERVAL '1 day') AS d(day) "
    "LEFT JOIN rpt.v_daily_sales v ON v.order_date = d.day GROUP BY 1 ORDER BY 1",
    # a materialized view, a percentile, a semi-join
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_pct) FROM rpt.mv_product_margin",
    "SELECT c.segment, count(*) FROM crm.customers c WHERE EXISTS (SELECT 1 FROM sales.orders o "
    "WHERE o.customer_id = c.customer_id AND o.channel = 'web') GROUP BY c.segment",
    # self-reference (referred_by -> customer_id) and a nullable FK to stores
    "SELECT r.customer_code AS referrer, count(*) FROM crm.customers c "
    "JOIN crm.customers r ON r.customer_id = c.referred_by GROUP BY 1 ORDER BY 2 DESC",
    # legacy: a numeric date in yyyymmdd, text to number
    "SELECT f3 / 100 AS yyyymm, sum(f7) FROM legacy.tb_trx02 WHERE f8 = 'OK' GROUP BY 1",
    "SELECT c01, to_number(nullif(trim(c07), ''), '999999999D99') FROM legacy.tb_mst01",
    # fx rates joined twice to currencies (two FKs to one key)
    "SELECT f.rate_date, a.currency_code, b.currency_code, f.rate FROM finance.fx_rates f "
    "JOIN finance.currencies a ON a.currency_code = f.from_ccy "
    "JOIN finance.currencies b ON b.currency_code = f.to_ccy",
]


async def _prepare(factory, fake_llm, fake_embedder):
    from test_profiling_pg import _introspect, _settings

    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["legacy.tb_trx02", "crm.customers", "store.stores",
                                      "rpt.v_legacy_transactions"]))
        ds = await s.scalar(sa.text("SELECT id FROM catalog.data_sources LIMIT 1"))
        # D48: a model hint counts once its claim test passed; a refuted one never does,
        # even with a passing test (f9 -> store_code passed at 98 % in Stage 1)
        for col, table, to_col, status in [("f2", "customers", "customer_code", "inferred"),
                                           ("f9", "stores", "store_code", "refuted")]:
            s.add(JoinHint(data_source_id=ds, from_schema="legacy", from_table="tb_trx02",
                           from_columns=[col], to_schema="crm" if table == "customers"
                           else "store", to_table=table, to_columns=[to_col], source="llm",
                           status=status, confidence=0.8))
        await s.commit()
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg",
                                                          tables=["sales.orders"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    return settings, ds


@pytest.mark.asyncio
async def test_guard_and_executor_on_demo_pg(pg_session_factory, fake_llm, fake_embedder):
    from test_profiling_pg import _target

    factory = pg_session_factory
    settings, ds = await _prepare(factory, fake_llm, fake_embedder)
    async with factory() as s:
        catalog, ds_id = await load_guard_catalog(s, "demo_pg")
    assert ds_id == ds
    kinds = {e.kind for e in catalog.edges}
    assert {"fk", "hint", "lineage"} <= kinds and catalog.is_unique("crm.customers.customer_code")
    guard = PostgresGuard(catalog, row_limit=1000)
    target = _target()

    # the realistic suite: accepted, and it runs
    for sql in SUITE:
        g = guard.check(sql)
        assert g.ok, (sql, [str(v) for v in g.errors])
        r = execute(target, g, row_limit=1000)
        assert r.ok, (sql, r.error, g.sql)
        assert r.columns

    # D48 on the real store: the inferred hint with a passed test joins; lineage carries
    # it to the view; the refuted hint and the bare Stage 1 candidate do not
    hint = guard.check("SELECT c.segment, sum(t.f7) FROM legacy.tb_trx02 t "
                       "JOIN crm.customers c ON c.customer_code = t.f2 GROUP BY 1")
    assert hint.ok and [j.kind for j in hint.joins] == ["hint"]
    assert execute(target, hint, row_limit=1000).ok
    view = guard.check("SELECT c.segment, sum(v.amount) FROM rpt.v_legacy_transactions v "
                       "JOIN crm.customers c ON c.customer_code = v.customer_code GROUP BY 1")
    assert view.ok and [j.kind for j in view.joins] == ["hint_via_view"], view.errors
    refuted = guard.check("SELECT s.city, count(*) FROM legacy.tb_trx02 t "
                          "JOIN store.stores s ON s.store_code = t.f9 GROUP BY 1")
    assert not refuted.ok and refuted.errors[0].code == "unverified_join"

    # the statement timeout and the row cap
    fan = guard.check("SELECT count(*) FROM sales.order_lines a "
                      "JOIN sales.order_lines b ON a.product_id = b.product_id")
    assert not fan.ok and [j.kind for j in fan.joins] == ["fan_out"]  # rows would multiply
    slow = guard.check("SELECT count(*) FROM generate_series(1, 200000000) AS g(x)")
    assert slow.ok
    timed = execute(target, slow, row_limit=1000, statement_timeout_s=1)
    assert timed.status == "timeout" and 900 <= timed.elapsed_ms < 10_000
    capped = PostgresGuard(catalog, row_limit=5).check("SELECT order_id FROM sales.orders")
    r = execute(target, capped, row_limit=5)
    assert r.ok and len(r.rows) == 5 and r.row_cap_reached
    own_limit = PostgresGuard(catalog, row_limit=5).check(
        "SELECT order_id FROM sales.orders LIMIT 3")
    r = execute(target, own_limit, row_limit=5)
    assert r.ok and len(r.rows) == 3 and not r.row_cap_reached

    # defence in depth: a statement forced past the guard meets the read-only session
    forced = GuardResult("postgres", "x", sql="CREATE TABLE public.gaia_forced (a int)")
    r = execute(target, forced, row_limit=10)
    assert r.status == "error" and "read-only transaction" in r.error

    # every run is recorded, pinned to the snapshot and the semantics it read
    async with factory() as s:
        store = QueryStore(s)
        ok_run = await store.record(data_source_id=ds, snapshot_id=catalog.snapshot_id,
                                    guard=capped, execution=execute(target, capped, row_limit=5),
                                    origin="test", question="five orders")
        bad = guard.check("DELETE FROM sales.orders")
        rejected = await store.record(data_source_id=ds, snapshot_id=catalog.snapshot_id,
                                      guard=bad, execution=None, origin="test")
        await s.commit()
        rows = {r.id: r for r in (await s.scalars(sa.select(QueryRun))).all()}
    run = rows[ok_run.id]
    assert run.status == "ok" and run.row_count == 5 and run.row_cap_reached
    assert run.snapshot_id == uuid.UUID(catalog.snapshot_id)
    assert run.semantic_versions["sales.orders"]["version"] == 1
    assert run.executed_sql == capped.sql and run.guard["tables"] == ["sales.orders"]
    no = rows[rejected.id]
    assert no.status == "rejected" and no.executed_sql is None
    assert "statement_type" in no.error
