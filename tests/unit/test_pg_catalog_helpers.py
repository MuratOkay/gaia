"""Pure helpers of the PostgreSQL catalog reader (no database needed)."""

from __future__ import annotations

import pytest

from gaia.dialects.postgres.catalog import _strip_check, decode_typmod, normalize_type


@pytest.mark.parametrize(
    ("formatted", "expected"),
    [
        ("character varying(20)", "varchar(20)"),
        ("character varying", "varchar"),
        ("character(1)", "char(1)"),
        ("bpchar", "char"),
        ("timestamp without time zone", "timestamp"),
        ("timestamp with time zone", "timestamptz"),
        ("timestamp(3) with time zone", "timestamptz(3)"),
        ("time without time zone", "time"),
        ("time(0) with time zone", "timetz(0)"),
        ("character varying(20)[]", "varchar(20)[]"),
        ("integer[][]", "integer[][]"),
        ("numeric(14,2)", "numeric(14,2)"),
        ("double precision", "double precision"),
        ("sales.order_status", "sales.order_status"),
        ("bit varying(8)", "varbit(8)"),
    ],
)
def test_normalize_type(formatted, expected):
    assert normalize_type(formatted) == expected


def test_decode_typmod():
    assert decode_typmod("varchar", 24) == (20, None, None)
    assert decode_typmod("bpchar", 5) == (1, None, None)
    assert decode_typmod("numeric", ((14 << 16) | 2) + 4) == (None, 14, 2)
    assert decode_typmod("numeric", ((5 << 16) | 0x7FE) + 4) == (None, 5, -2)  # PG 15+
    assert decode_typmod("timestamptz", 3) == (None, 3, None)
    assert decode_typmod("int4", -1) == (None, None, None)
    assert decode_typmod("varchar", -1) == (None, None, None)


def test_strip_check():
    assert _strip_check("CHECK (quantity > 0)") == "(quantity > 0)"
    assert _strip_check("CHECK ((salary > 0::numeric)) NOT VALID") == "((salary > 0::numeric))"
    assert _strip_check(None) is None
