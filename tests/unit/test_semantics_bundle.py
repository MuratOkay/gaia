"""Table bundles: content per PHASES, stable refs, facts for scoring, determinism."""

from __future__ import annotations

import json

from gaia.semantics.bundle import build_bundle, build_catalog_index


def _table(snapshot, qualified):
    return next(t for t in snapshot.tables if t.qualified_name == qualified)


def test_bundle_contains_phase2_facts(sample_snapshot):
    doc = build_bundle(_table(sample_snapshot, "dbo.orders")).document

    assert doc["table"] == "dbo.orders"
    assert doc["estimated_rows"] == 50_000
    assert [c["name"] for c in doc["columns"]] == [
        "order_id",
        "customer_id",
        "order_date",
        "order_total",
        "status",
        "notes",
        "source_system",
    ]
    status = next(c for c in doc["columns"] if c["name"] == "status")
    assert status == {"name": "status", "type": "char(1)", "nullable": False, "default": "('N')"}
    assert doc["primary_key"] == {"ref": "pk:PK_orders", "columns": ["order_id"]}
    fk = doc["foreign_keys"][0]
    assert fk["ref"] == "fk:FK_orders_customers"
    assert fk["references"] == "dbo.customers"
    assert fk["referenced_columns"] == ["customer_id"]
    checks = {c["ref"]: c for c in doc["check_constraints"]}
    assert checks["ck:CK_orders_status"]["not_trusted"] is True
    assert "not_trusted" not in checks["ck:CK_orders_total_nonneg"]


def test_nullable_always_shown_and_false_flags_omitted(sample_snapshot):
    doc = build_bundle(_table(sample_snapshot, "dbo.orders")).document
    order_id = doc["columns"][0]
    assert order_id == {"name": "order_id", "type": "int", "nullable": False, "identity": True}
    assert list(order_id)[:3] == ["name", "type", "nullable"]


def test_check_columns_recovered_from_definition(sample_snapshot):
    bundle = build_bundle(_table(sample_snapshot, "dbo.orders"))
    checks = {c["ref"]: c for c in bundle.document["check_constraints"]}

    assert checks["ck:CK_orders_status"]["columns"] == ["status"]
    assert bundle.facts["status"].checks == ["ck:CK_orders_status"]
    assert bundle.facts["status"].checks_trusted is False  # NOT TRUSTED
    assert bundle.facts["order_total"].checks_trusted is True


def test_pk_and_unique_constraint_indexes_not_duplicated(sample_snapshot):
    bundle = build_bundle(_table(sample_snapshot, "dbo.customers"))
    assert "indexes" not in bundle.document  # only PK/UQ backing indexes exist
    assert bundle.facts["email"].in_unique is True
    assert bundle.facts["customer_id"].in_primary_key is True


def test_index_key_and_included_columns(sample_snapshot):
    doc = build_bundle(_table(sample_snapshot, "dbo.orders")).document
    (ix,) = doc["indexes"]
    assert ix["columns"] == ["customer_id", "order_date"]
    assert ix["included"] == ["order_total"]


def test_composite_pk_facts(sample_snapshot):
    bundle = build_bundle(_table(sample_snapshot, "dbo.order_lines"))
    assert bundle.facts["order_id"].primary_key_size == 2
    assert bundle.facts["order_id"].foreign_keys == ["fk:FK_order_lines_orders"]
    fk = bundle.document["foreign_keys"][0]
    assert fk["on_delete"] == "CASCADE"


def test_view_bundle_has_no_key_sections(sample_snapshot):
    doc = build_bundle(_table(sample_snapshot, "rpt.v_daily_sales")).document
    assert doc["kind"] == "view"
    for key in ("primary_key", "foreign_keys", "indexes", "check_constraints", "estimated_rows"):
        assert key not in doc


def test_ref_validity_is_column_specific(sample_snapshot):
    bundle = build_bundle(_table(sample_snapshot, "dbo.orders"))
    assert bundle.ref_is_valid_for("fk:FK_orders_customers", "customer_id")
    assert not bundle.ref_is_valid_for("fk:FK_orders_customers", "order_total")
    assert bundle.ref_is_valid_for("ix:IX_orders_customer_date", "order_total")  # included
    assert bundle.ref_is_valid_for("default", "status")
    assert not bundle.ref_is_valid_for("default", "order_total")  # no default
    assert not bundle.ref_is_valid_for("comment", "status")  # no comment
    assert not bundle.ref_is_valid_for("ck:does_not_exist", "status")
    assert bundle.ref_is_valid_for("sibling_columns", "notes")


def test_render_is_deterministic_and_valid_json(sample_snapshot):
    a = build_bundle(_table(sample_snapshot, "dbo.orders")).render()
    b = build_bundle(_table(sample_snapshot, "dbo.orders")).render()
    assert a == b
    assert json.loads(a)["table"] == "dbo.orders"


def test_structure_hash_matches_snapshot(sample_snapshot):
    table = _table(sample_snapshot, "dbo.orders")
    assert build_bundle(table).structure_hash == table.structure_hash


def test_catalog_index_lists_objects_with_pk(sample_snapshot):
    index = build_catalog_index(sample_snapshot)
    text = index.render()
    assert "- dbo.order_lines (pk: order_id, line_no)" in text
    assert "- rpt.v_daily_sales [view]" in text
    assert index.has_table("dbo.products")
    assert index.columns["dbo.products"] == {"product_code", "product_name"}
