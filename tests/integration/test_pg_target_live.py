"""Live tests of the PostgreSQL dialect against the development target (D25).

Opt-in: set ``GAIA_TEST_PG_TARGET_URL`` to the *read-only* role's DSN of a
database seeded by ``infra/pg-target/seed.py``, e.g.

    GAIA_TEST_PG_TARGET_URL=postgresql://gaia_ro:<pw>@127.0.0.1:5434/gaia_target_pg

Assertions are independent of ``--scale``. Nothing is written to the target:
the probe's DDL attempt is denied and rolled back.
"""

from __future__ import annotations

import json
import os
from urllib.parse import unquote, urlsplit

import pytest

from gaia.core.config import TargetConfig

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")


@pytest.fixture(scope="module")
def target() -> TargetConfig:
    parts = urlsplit(URL)
    return TargetConfig(
        dialect="postgres",
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 5432,
        database=parts.path.lstrip("/"),
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


@pytest.fixture(scope="module")
def snapshot(target):
    from gaia.dialects.postgres.catalog import PostgresCatalogReader

    return PostgresCatalogReader(target).read_snapshot(data_source="demo_pg")


def _table(snapshot, name):
    schema, table = name.split(".")
    found = snapshot.table(schema, table)
    assert found is not None, name
    return found


def _col(snapshot, table, column):
    return next(c for c in _table(snapshot, table).columns if c.name == column)


def test_object_inventory(snapshot):
    counts = snapshot.to_dict()["counts"]
    assert counts == {"tables": 24, "views": 4, "columns": 213}
    assert snapshot.dialect == "postgres"
    assert all(not t.name.startswith("order_events_") for t in snapshot.tables)  # partitions folded


def test_column_mapping(snapshot):
    assert _col(snapshot, "crm.customers", "customer_id").is_identity  # GENERATED ... IDENTITY
    serial = _col(snapshot, "hr.employees", "employee_id")
    assert serial.is_identity and serial.default.startswith("nextval(")
    generated = _col(snapshot, "sales.order_lines", "line_subtotal")
    assert generated.is_computed and "round(" in generated.computed_definition
    assert generated.default is None
    email = _col(snapshot, "crm.customers", "email")
    assert email.data_type == "varchar(320)" and email.max_length == 320
    assert email.type_definition.startswith("domain crm.email_address: CHECK")
    status = _col(snapshot, "sales.orders", "status")
    assert status.data_type == "sales.order_status"
    assert status.type_definition.startswith("enum ('new', 'paid'")
    assert _col(snapshot, "crm.customers", "created_at").data_type == "timestamptz"
    cost = _col(snapshot, "product.products", "unit_cost")
    assert (cost.data_type, cost.precision, cost.scale) == ("numeric(12,2)", 12, 2)


def test_constraint_trust_and_enforcement(snapshot):
    (returns_fk,) = _table(snapshot, "sales.returns").relations
    assert returns_fk.is_not_trusted and not returns_fk.is_disabled
    invoice_fks = _table(snapshot, "finance.invoices").relations
    assert invoice_fks and all(r.is_disabled and not r.is_not_trusted for r in invoice_fks)
    checks = {c.name: c for c in _table(snapshot, "hr.employees").constraints if c.kind == "check"}
    salary = checks["ck_employees_salary_positive"]
    assert salary.is_not_trusted and salary.columns == ["salary"]
    assert not salary.definition.upper().startswith("CHECK")
    assert "NOT VALID" not in salary.definition


def test_indexes(snapshot):
    idx = {i.name: i for i in _table(snapshot, "sales.orders").indexes}
    cover = idx["ix_orders_customer_date"]
    assert [(c["name"], c["included"]) for c in cover.columns] == [
        ("customer_id", False), ("order_date", False), ("total_amount", True)]
    assert cover.columns[1]["descending"]
    cust = {i.name: i for i in _table(snapshot, "crm.customers").indexes}
    assert cust["ix_customers_lower_email"].columns[0]["name"].startswith("lower(")
    assert cust["ix_customers_active_segment"].filter_definition == "is_active"


def test_rows_partitions_views_comments(snapshot):
    events = _table(snapshot, "sales.order_events")
    assert events.partition_count == 4 and events.estimated_rows > 0
    assert _table(snapshot, "rpt.mv_product_margin").kind == "view"
    assert _table(snapshot, "rpt.v_legacy_transactions").kind == "view"
    assert _table(snapshot, "sales.orders").comment
    comments = sum(1 for t in snapshot.tables for c in t.columns if c.comment)
    assert comments == 2


def test_read_is_deterministic(target, snapshot):
    from gaia.dialects.postgres.catalog import PostgresCatalogReader

    again = PostgresCatalogReader(target).read_snapshot(data_source="demo_pg")
    a, b = snapshot.to_dict(), again.to_dict()
    for d in (a, b):
        d.pop("snapshot_id"), d.pop("captured_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_probe_proves_read_only(target):
    from gaia.dialects.postgres.probe import run_checks

    checks = {c.name: c for c in run_checks(target)}
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert "42501" in checks["security.read_only"].detail


def test_pg_bundle_scores_with_pg_vocabulary(snapshot):
    from gaia.semantics.bundle import build_bundle

    bundle = build_bundle(_table(snapshot, "crm.customers"), dialect=snapshot.dialect)
    assert bundle.facts["is_active"].dialect == "postgres"
    assert bundle.facts["segment"].type_definition.startswith("enum")
    assert "type_definition" in json.dumps(bundle.document)


def test_statement_timeout_cancels_and_session_survives(target):
    import time

    from gaia.dialects.errors import StatementTimeout
    from gaia.dialects.postgres.connection import session

    with session(target, autocommit=True, statement_timeout_s=1) as cur:
        started = time.perf_counter()
        with pytest.raises(StatementTimeout) as exc:
            cur.execute("SELECT pg_sleep(5)")
        assert 0.8 <= time.perf_counter() - started < 4.0
        assert exc.value.dialect == "postgres"
        cur.execute("SELECT 1")  # autocommit: the next statement is unaffected
        assert cur.fetchone()[0] == 1
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()[0] == "on"


def test_measurement_harness_on_the_worst_case_table(target, snapshot):
    import argparse

    from gaia.dialects.postgres.measure import PostgresMeasure
    from gaia.tools.measure_target import Budget, measure_table

    backend = PostgresMeasure(target, statement_timeout_s=60, maxdop=2)
    table = _table(snapshot, "legacy.tb_trx02")
    opts = argparse.Namespace(max_columns=60, sample_rows=1000, seed=42)
    with backend.session() as cur:
        before = backend.stats_inventory(cur)
        out = measure_table(backend, cur, table, opts, Budget(5))
        after = backend.stats_inventory(cur)
        support = {s["kind"]: s for s in backend.sampling_support(cur, 42)}

    exact = {e["column"]: e["value"] for e in out["measurements"] if e["op"] == "distinct_exact"}
    assert exact["f8"] == 3 and exact["f10"] == 2  # status and currency codes, by design
    assert exact["f1"] == out["measurements"][0]["value"]  # f1 is unique: distinct == count
    assert all("error" not in e.get("first", {}) for e in out["measurements"])
    assert next(e for e in out["measurements"] if e["op"] == "scan")["repeat"]["pages"] > 0
    assert before == after  # PostgreSQL: profiling leaves no statistics footprint
    assert support["partitioned table"]["supported"]
    assert support["materialized view"]["supported"]
    assert not support["view"]["supported"]
