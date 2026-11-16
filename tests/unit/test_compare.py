"""Result comparison for the golden runs (Phase 4D)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

from gaia.query.compare import Table, compare


def T(cols, rows):  # noqa: N802
    return Table(cols, rows)


def test_columns_match_by_value_not_name_and_extras_are_allowed():
    ref = T(["city", "amount"], [("Adana", D("3934224.77"))])
    got = T(["month", "store_city", "total"], [("2025-12", "Adana", D("3934224.77"))])
    c = compare(ref, got)
    assert c.match and c.mapping == {"city": "store_city", "amount": "total"}


def test_numbers_equal_at_the_precision_shown():
    ref = T(["m", "rate"], [(date(2025, 1, 1), D("39.0297"))])
    assert compare(ref, T(["m", "r"], [(date(2025, 1, 1), D("39.03"))])).match
    assert compare(ref, T(["m", "r"], [(datetime(2025, 1, 1, tzinfo=UTC),
                                        39.0297)])).match
    assert not compare(ref, T(["m", "r"], [(date(2025, 1, 1), D("39.04"))])).match
    small = T(["x"], [(D("0.0234"),)])
    assert not compare(small, T(["x"], [(D("0.03"),)])).match
    assert compare(T(["n"], [(252,)]), T(["n"], [(D("252"),)])).match
    assert not compare(T(["n"], [(252,)]), T(["n"], [(251,)])).match


def test_rows_are_a_multiset_unless_order_matters():
    ref = T(["c", "n"], [("a", 1), ("b", 2)])
    swapped = T(["c", "n"], [("b", 2), ("a", 1)])
    assert compare(ref, swapped).match
    assert not compare(ref, swapped, order_matters=True).match
    assert not compare(ref, T(["c", "n"], [("a", 1)])).match
    assert not compare(ref, T(["c", "n"], [("a", 1), ("b", 3)])).match
    # the rows must line up: the right values in the wrong combination are wrong
    assert not compare(ref, T(["c", "n"], [("a", 2), ("b", 1)])).match


def test_one_identifier_is_enough():
    ref = T(["sku", "name", "product_id", "qty"], [("TY-1", "Notebook", 62, 149)])
    assert compare(ref, T(["name", "quantity"], [("Notebook", 149)]),
                   identify_by=["sku", "name", "product_id"]).match
    assert compare(ref, T(["product_id", "q"], [(62, 149)]),
                   identify_by=["sku", "name", "product_id"]).match
    assert not compare(ref, T(["quantity"], [(149,)]),
                       identify_by=["sku", "name", "product_id"]).match
    assert not compare(ref, T(["name", "quantity"], [("Notebook", 149)])).match  # all needed


def test_ties_at_the_cut_are_interchangeable():
    # ten with the most: the 2nd and 3rd of the extended list tie at the cut (limit 2)
    ref = T(["sku", "qty"], [("A", 10), ("B", 5)])
    extended = [("A", 10), ("B", 5), ("C", 5), ("D", 4)]
    assert compare(ref, T(["sku", "qty"], [("A", 10), ("C", 5)]), rank_by="qty",
                   extended=extended).match
    assert compare(ref, T(["sku", "qty"], [("A", 10), ("C", 5)]), rank_by="qty",
                   extended=extended, order_matters=True).match
    assert not compare(ref, T(["sku", "qty"], [("A", 10), ("D", 4)]), rank_by="qty",
                       extended=extended).match
    assert not compare(ref, T(["sku", "qty"], [("B", 5), ("C", 5)]), rank_by="qty",
                       extended=extended).match  # A is above the cut and missing


def test_ties_inside_an_ordered_list_may_come_in_either_order():
    ref = T(["sku", "qty"], [("A", 10), ("B", 5), ("C", 5), ("D", 1)])
    got = T(["sku", "qty"], [("A", 10), ("C", 5), ("B", 5), ("D", 1)])
    assert compare(ref, got, order_matters=True, rank_by="qty").match
    assert not compare(ref, got, order_matters=True).match


def test_empty_results_and_text_numbers():
    assert compare(T(["n"], []), T(["x"], [])).match
    assert compare(T(["n"], [(D("12.50"),)]), T(["n"], [("12.5",)])).match


def test_rounding_across_a_bucket_boundary():
    # 2.4499 shown to two places is 2.45: equal, however a coarser rounding would split them
    assert compare(T(["x"], [(D("2.45"),)]), T(["x"], [(D("2.4499"),)])).match
    assert compare(T(["x"], [(D("3.00"),)]), T(["x"], [(D("2.9999"),)])).match
    assert not compare(T(["x"], [(D("3.00"),)]), T(["x"], [(D("2.99"),)])).match
