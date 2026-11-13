"""Hashing determinism and the properties incremental discovery depends on."""

from __future__ import annotations

from datetime import UTC, datetime

from gaia.introspect.hashing import (
    canonical_json,
    data_hash,
    is_meaningful_change,
    normalize_text,
    profile_hash,
    sha256_of,
    structure_hash,
)
from gaia.introspect.snapshot import ColumnSnapshot, Snapshot, TableSnapshot


def _column(**overrides) -> ColumnSnapshot:
    base = {
        "name": "amount",
        "ordinal": 3,
        "data_type": "decimal(14,6)",
        "nullable": False,
        "comment": "Order amount",
    }
    base.update(overrides)
    return ColumnSnapshot(**base)


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_has_no_insignificant_whitespace():
    assert canonical_json({"a": [1, 2], "b": "x"}) == '{"a":[1,2],"b":"x"}'


def test_canonical_json_preserves_non_ascii():
    # ensure_ascii=False keeps hashes stable regardless of locale.
    assert "Şişli" in canonical_json({"city": "Şişli"})


def test_sha256_is_stable_across_calls():
    assert sha256_of({"a": 1}) == sha256_of({"a": 1})


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  a   b \n c ") == "a b c"


def test_normalize_text_preserves_none():
    assert normalize_text(None) is None


def test_normalize_text_maps_blank_to_none():
    assert normalize_text("   ") is None


def test_structure_and_data_hashes_use_different_salts():
    payload = {"x": 1}
    assert structure_hash(payload) != data_hash(payload)


def test_profile_hash_includes_profiler_and_params():
    base = profile_hash({"rows": 1}, profiler_version="v1", params={"seed": 42})
    assert base != profile_hash({"rows": 1}, profiler_version="v2", params={"seed": 42})
    assert base != profile_hash({"rows": 1}, profiler_version="v1", params={"seed": 7})
    assert base == profile_hash({"rows": 1}, profiler_version="v1", params={"seed": 42})


def test_is_meaningful_change_treats_missing_old_as_change():
    assert is_meaningful_change(None, "abc") is True
    assert is_meaningful_change("abc", "abc") is False
    assert is_meaningful_change("abc", "def") is True


# -- column-level hashing ---------------------------------------------------


def test_column_hash_ignores_ordinal():
    """Reordering columns is not a semantic change."""
    assert _column(ordinal=3).structure_hash == _column(ordinal=9).structure_hash


def test_column_hash_changes_on_type():
    assert _column().structure_hash != _column(data_type="int").structure_hash


def test_column_hash_changes_on_nullability():
    assert _column().structure_hash != _column(nullable=True).structure_hash


def test_column_hash_changes_on_comment():
    assert _column().structure_hash != _column(comment="Gross amount").structure_hash


def test_column_hash_is_insensitive_to_comment_whitespace():
    a = _column(comment="Order  amount")
    b = _column(comment="Order amount")
    assert a.structure_hash == b.structure_hash
