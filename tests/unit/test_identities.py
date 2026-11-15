"""Cross-column identities on sampled rows."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from decimal import Decimal

from gaia.profiling.identities import find_identities


def _rows(n=300, seed=3):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        qty = rng.randint(1, 10)
        price = Decimal(rng.randint(100, 50_000)) / 100
        tax = (price * qty * Decimal("0.2")).quantize(Decimal("0.01"))
        ordered = datetime(2025, 1, 1) + timedelta(hours=i)
        shipped = ordered + timedelta(hours=rng.randint(1, 48))
        rows.append((i, qty, price, price * qty, tax, price * qty + tax, ordered, shipped,
                     rng.randint(1, 9)))
    return rows


COLS = ["id", "f5", "f6", "f7", "tax", "total", "ordered_at", "shipped_at", "noise"]


def test_arithmetic_and_temporal_identities():
    found = {r.expression: r for r in find_identities(
        COLS, _rows(), numeric=["id", "f5", "f6", "f7", "tax", "total", "noise"],
        temporal=["ordered_at", "shipped_at"])}
    assert "f7 = f5 × f6" in found and found["f7 = f5 × f6"].holds_rate == 1.0
    assert "total = f7 + tax" in found
    assert "ordered_at <= shipped_at" in found
    assert found["ordered_at <= shipped_at"].kind == "temporal_order"
    assert not any("noise" in e for e in found)


def test_needs_enough_rows_and_a_high_rate():
    rows = _rows(30)
    assert find_identities(COLS, rows, numeric=["f5", "f6", "f7"], temporal=[]) == []
    broken = [(*r[:3], r[3] + (Decimal(5) if i % 10 == 0 else 0), *r[4:])
              for i, r in enumerate(_rows())]
    assert find_identities(COLS, broken, numeric=["f5", "f6", "f7"], temporal=[]) == []


def test_equal_timestamps_say_nothing():
    t = datetime(2025, 1, 1)
    rows = [(t, t) for _ in range(100)]
    assert find_identities(["a", "b"], rows, numeric=[], temporal=["a", "b"]) == []
