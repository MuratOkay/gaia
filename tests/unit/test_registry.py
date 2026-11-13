"""Dialect registry resolves implementations and fails loudly otherwise."""

from __future__ import annotations

import pytest

from gaia.dialects.registry import DialectRegistry


class _FakeReader:
    dialect = "fake"

    def read_snapshot(self) -> dict:
        return {"tables": []}


def test_unknown_dialect_raises():
    registry = DialectRegistry()
    with pytest.raises(LookupError, match="no metadata reader"):
        registry.reader("oracle")


def test_register_and_resolve():
    registry = DialectRegistry()
    registry.register_reader("fake", _FakeReader)

    reader = registry.reader("fake")

    assert reader.dialect == "fake"
    assert reader.read_snapshot() == {"tables": []}
    assert registry.dialects == ["fake"]
