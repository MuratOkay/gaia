"""CLI scope resolution (PHASES decision 1: one table + FK neighbours)."""

from __future__ import annotations

import pytest

from gaia.semantics.service import ScopeError, select_tables


def _names(tables):
    return [t.qualified_name for t in tables]


def test_everything_by_default_sorted(sample_snapshot):
    assert _names(select_tables(sample_snapshot)) == [
        "dbo.customers",
        "dbo.order_lines",
        "dbo.orders",
        "dbo.products",
        "rpt.v_daily_sales",
    ]


def test_single_table_with_neighbours_follows_fks_both_ways(sample_snapshot):
    picked = select_tables(sample_snapshot, tables=["dbo.orders"], with_neighbours=True)
    # orders -> customers (outgoing), order_lines -> orders (incoming)
    assert _names(picked) == ["dbo.customers", "dbo.order_lines", "dbo.orders"]


def test_bare_table_name_and_case_insensitive(sample_snapshot):
    assert _names(select_tables(sample_snapshot, tables=["ORDERS"])) == ["dbo.orders"]


def test_schema_filter_and_limit(sample_snapshot):
    assert _names(select_tables(sample_snapshot, schemas=["rpt"])) == ["rpt.v_daily_sales"]
    assert len(select_tables(sample_snapshot, limit=2)) == 2


def test_unknown_names_fail_loudly(sample_snapshot):
    with pytest.raises(ScopeError, match="nope"):
        select_tables(sample_snapshot, tables=["dbo.nope"])
    with pytest.raises(ScopeError, match="xx"):
        select_tables(sample_snapshot, schemas=["xx"])
