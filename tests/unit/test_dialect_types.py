"""Dialect type vocabularies and the scorer's role/type fit.

The SQL Server vocabulary must reproduce the Phase 2 scorer exactly: the
original implementation is embedded below as an oracle and compared on every
role x type x naming x key combination. PostgreSQL gets its own vocabulary,
where e.g. ``timestamp`` is a date-time (in SQL Server it is a row version).
"""

from __future__ import annotations

import itertools
import re

import pytest

from gaia.dialects.types import dialect_types, type_info
from gaia.semantics import scoring
from gaia.semantics.bundle import ColumnFacts
from gaia.semantics.schemas import ColumnMeaning
from gaia.semantics.scoring import _role_type_fit

ROLES = [
    "identifier", "reference", "measure", "dimension", "timestamp", "flag", "code", "free-text",
]

# -- the Phase 2 implementation, verbatim (oracle) ---------------------------------

_NUMERIC = {
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "decimal",
    "numeric",
    "money",
    "smallmoney",
    "float",
    "real",
}
_TEMPORAL = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
_TEXT = {"char", "varchar", "nchar", "nvarchar", "text", "ntext", "sysname"}
# snake_case suffixes (any case) and camelCase suffixes (case-sensitive, so
# "Valid" or "Piano" do not read as key-like).
_KEYLIKE_SNAKE = re.compile(r"(^|_)(id|key|code|no|num|number|nr|ref|guid|uuid)$", re.IGNORECASE)
_KEYLIKE_CAMEL = re.compile(r"[a-z0-9](Id|ID|Key|No|Nr|Code|Ref|Guid|Uuid)$")
_DATELIKE_NAME = re.compile(r"date|time|year|month|period|day|_dt$|_ts$", re.IGNORECASE)

_UNITLESS_ROLES = {"identifier", "reference", "flag", "free-text", "code"}



def _base_type(data_type: str) -> str:
    return data_type.split("(", 1)[0].strip().lower()


def _type_length(data_type: str) -> int | None:
    m = re.search(r"\(([^)]*)\)", data_type)
    if not m:
        return None
    arg = m.group(1).split(",")[0].strip().lower()
    if arg == "max":
        return -1
    try:
        return int(arg)
    except ValueError:
        return None


def _phase2_role_type_fit(meaning: ColumnMeaning, facts: ColumnFacts) -> tuple[bool, str]:
    base = _base_type(facts.data_type)
    length = _type_length(facts.data_type)
    name = facts.name
    role = meaning.role
    keyish = facts.in_primary_key or facts.in_unique or facts.is_identity or facts.is_rowguid
    keylike_name = bool(_KEYLIKE_SNAKE.search(name) or _KEYLIKE_CAMEL.search(name))

    if role == "measure":
        return base in _NUMERIC, f"measure requires a numeric type, got {base}"
    if role == "timestamp":
        ok = base in _TEMPORAL or (base in {"int", "bigint"} and bool(_DATELIKE_NAME.search(name)))
        return ok, f"timestamp requires a temporal type or a date-key integer, got {base}"
    if role == "flag":
        ok = base == "bit" or base == "tinyint" or (base in _TEXT and length == 1)
        return ok, f"flag requires bit, tinyint or a one-character text, got {facts.data_type}"
    if role == "identifier":
        ok = keyish or base == "uniqueidentifier" or keylike_name
        return ok, "identifier requires a key, identity, uniqueidentifier or key-like name"
    if role == "reference":
        ok = bool(facts.foreign_keys) or keylike_name or base == "uniqueidentifier"
        return ok, "reference requires a foreign key or a key-like name"
    if role == "free-text":
        return base in _TEXT or base == "xml", f"free-text requires a text type, got {base}"
    if role == "code":
        ok = (base in _TEXT and (length is not None and 0 < length <= 50)) or base in {
            "tinyint",
            "smallint",
            "int",
            "char",
            "nchar",
        }
        return ok, f"code requires a short text or small integer type, got {facts.data_type}"
    # dimension: anything but binary/large-object types
    ok = base not in {"varbinary", "binary", "image", "timestamp", "rowversion"}
    return ok, f"dimension does not fit binary type {base}"




# -- helpers ------------------------------------------------------------------------


def _meaning(role: str) -> ColumnMeaning:
    return ColumnMeaning(name="c", business_name="C", purpose="p", role=role, confidence=0.5)


MSSQL_TYPES = [
    "tinyint", "smallint", "int", "bigint", "decimal(18,2)", "numeric(9,0)", "money",
    "smallmoney", "float(53)", "real", "date", "datetime", "datetime2(7)", "smalldatetime",
    "datetimeoffset(7)", "time(7)", "char(1)", "char(10)", "nchar(1)", "nchar(60)", "varchar(1)",
    "varchar(50)", "varchar(51)", "varchar(max)", "nvarchar(1)", "nvarchar(50)", "nvarchar(max)",
    "text", "ntext", "sysname", "bit", "uniqueidentifier", "xml", "varbinary(max)", "binary(8)",
    "image", "timestamp", "rowversion", "sql_variant", "geography", "hierarchyid", "dbo.Phone",
]
NAMES = ["amount", "customer_id", "orderNo", "order_date", "created_ts", "status", "x"]


@pytest.mark.parametrize("data_type", MSSQL_TYPES)
def test_mssql_vocabulary_reproduces_phase2_exactly(data_type):
    for role, name, keyish, fk in itertools.product(ROLES, NAMES, (False, True), (False, True)):
        facts = ColumnFacts(
            name=name, data_type=data_type, nullable=True, in_primary_key=keyish,
            foreign_keys=["fk:x"] if fk else [],
        )
        assert _role_type_fit(_meaning(role), facts) == _phase2_role_type_fit(
            _meaning(role), facts
        ), (role, name, data_type, keyish, fk)


def _pg_fit(role: str, data_type: str, name: str = "x", type_definition: str | None = None):
    facts = ColumnFacts(
        name=name, data_type=data_type, nullable=True, dialect="postgres",
        type_definition=type_definition,
    )
    return _role_type_fit(_meaning(role), facts)[0]


def test_postgres_vocabulary():
    assert _pg_fit("flag", "boolean")
    assert _pg_fit("flag", "char(1)")
    assert not _pg_fit("flag", "integer")
    assert _pg_fit("timestamp", "timestamptz")
    assert _pg_fit("timestamp", "timestamp(3)")
    assert _pg_fit("dimension", "timestamp")  # a date-time, not SQL Server's row version
    assert _pg_fit("timestamp", "integer", name="order_date")  # yyyymmdd key
    assert not _pg_fit("timestamp", "integer", name="f3")
    assert _pg_fit("measure", "numeric(14,2)")
    assert _pg_fit("measure", "double precision")
    assert not _pg_fit("measure", "integer[]")  # arrays are not measures
    assert _pg_fit("free-text", "jsonb")
    assert _pg_fit("free-text", "text")
    assert _pg_fit("identifier", "uuid")
    assert not _pg_fit("dimension", "bytea")
    assert _pg_fit("code", "sales.order_status", type_definition="enum ('new', 'paid')")
    assert not _pg_fit("code", "sales.order_status")  # unknown type without definition
    assert _pg_fit("code", "varchar(12)")


def test_same_name_different_meaning_across_dialects():
    assert dialect_types("mssql").binary >= {"timestamp"}
    assert "timestamp" in dialect_types("postgres").temporal
    assert type_info("mssql", "timestamp").is_binary
    assert type_info("postgres", "timestamp").is_temporal


def test_unknown_dialect_falls_back_to_phase2_vocabulary():
    assert dialect_types("oracle") is dialect_types("mssql")
    assert dialect_types(None) is dialect_types("mssql")


def test_name_patterns_are_dialect_neutral():
    assert scoring._KEYLIKE_SNAKE.search("customer_id")
    assert scoring._KEYLIKE_CAMEL.search("orderNo")
    assert scoring._DATELIKE_NAME.search("posted_on_date")
    assert not scoring._KEYLIKE_SNAKE.search("f1")
