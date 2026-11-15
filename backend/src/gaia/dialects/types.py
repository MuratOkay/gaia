"""Dialect type vocabularies for dialect-neutral reasoning about data types.

The scorer checks whether a claimed role fits a column's data type ("a measure
needs a numeric type"). Type names are dialect-specific, and sometimes the same
name means different things: SQL Server ``timestamp`` is a binary row version,
PostgreSQL ``timestamp`` is a date and time. Each dialect therefore declares its
own vocabulary here, and the scorer asks questions of a ``TypeInfo`` instead of
matching raw names.

The SQL Server vocabulary reproduces the Phase 2 scorer's sets exactly; a unit
test compares every role/type combination against the original implementation,
so this refactor cannot move a single SQL Server score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

__all__ = ["DialectTypes", "TypeInfo", "base_type", "dialect_types", "type_info", "type_length"]


@dataclass(frozen=True)
class DialectTypes:
    dialect: str
    numeric: frozenset[str]
    temporal: frozenset[str]
    text: frozenset[str]
    # bit-like types that can hold a yes/no flag (besides one-character text)
    flag: frozenset[str]
    # types accepted for a coded value regardless of length
    code: frozenset[str]
    # integer types that can hold a date key such as 20250131
    date_key: frozenset[str]
    uuid: frozenset[str]
    # non-text types that can hold unstructured or semi-structured text
    document: frozenset[str]
    # types no business attribute should be stored in
    binary: frozenset[str]


_MSSQL = DialectTypes(
    dialect="mssql",
    numeric=frozenset(
        {
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
    ),
    temporal=frozenset(
        {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
    ),
    text=frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext", "sysname"}),
    flag=frozenset({"bit", "tinyint"}),
    code=frozenset({"tinyint", "smallint", "int", "char", "nchar"}),
    date_key=frozenset({"int", "bigint"}),
    uuid=frozenset({"uniqueidentifier"}),
    document=frozenset({"xml"}),
    binary=frozenset({"varbinary", "binary", "image", "timestamp", "rowversion"}),
)

_POSTGRES = DialectTypes(
    dialect="postgres",
    numeric=frozenset(
        {
            "smallint",
            "integer",
            "int",
            "bigint",
            "int2",
            "int4",
            "int8",
            "numeric",
            "decimal",
            "real",
            "double precision",
            "float4",
            "float8",
            "money",
        }
    ),
    temporal=frozenset({"date", "timestamp", "timestamptz", "time", "timetz"}),
    text=frozenset({"char", "varchar", "text", "bpchar", "citext", "name"}),
    flag=frozenset({"boolean", "bool"}),
    code=frozenset({"smallint", "integer", "int", "int2", "int4", "char", "bpchar"}),
    date_key=frozenset({"integer", "int", "int4", "bigint", "int8"}),
    uuid=frozenset({"uuid"}),
    document=frozenset({"xml", "json", "jsonb"}),
    binary=frozenset({"bytea"}),
)

_BY_NAME = {"mssql": _MSSQL, "postgres": _POSTGRES, "postgresql": _POSTGRES}

_LENGTH = re.compile(r"\(([^)]*)\)")


def dialect_types(dialect: str | None) -> DialectTypes:
    """Vocabulary for a dialect; unknown dialects fall back to SQL Server's,
    which is what every Phase 2 score was computed with."""
    return _BY_NAME.get((dialect or "mssql").lower(), _MSSQL)


def base_type(data_type: str) -> str:
    """``varchar(20)`` -> ``varchar``; ``timestamptz(3)`` -> ``timestamptz``."""
    return data_type.split("(", 1)[0].strip().lower()


def type_length(data_type: str) -> int | None:
    """Declared length of char types; -1 for (max); None if not applicable."""
    m = _LENGTH.search(data_type)
    if not m:
        return None
    arg = m.group(1).split(",")[0].strip().lower()
    if arg == "max":
        return -1
    try:
        return int(arg)
    except ValueError:
        return None


@dataclass(frozen=True)
class TypeInfo:
    """What a column's declared type allows, in one dialect's vocabulary."""

    base: str
    length: int | None
    types: DialectTypes
    is_array: bool = False
    is_enum: bool = False

    @property
    def is_numeric(self) -> bool:
        return not self.is_array and self.base in self.types.numeric

    @property
    def is_temporal(self) -> bool:
        return not self.is_array and self.base in self.types.temporal

    @property
    def is_text(self) -> bool:
        return not self.is_array and self.base in self.types.text

    @property
    def is_uuid(self) -> bool:
        return not self.is_array and self.base in self.types.uuid

    @property
    def is_binary(self) -> bool:
        return not self.is_array and self.base in self.types.binary

    def fits_flag(self) -> bool:
        return not self.is_array and (
            self.base in self.types.flag or (self.is_text and self.length == 1)
        )

    def fits_code(self) -> bool:
        if self.is_array:
            return False
        if self.is_enum:
            return True
        return (self.is_text and self.length is not None and 0 < self.length <= 50) or (
            self.base in self.types.code
        )

    def fits_date_key(self) -> bool:
        return not self.is_array and self.base in self.types.date_key

    def fits_free_text(self) -> bool:
        return not self.is_array and (self.is_text or self.base in self.types.document)


@cache
def _type_info(dialect: str, data_type: str, is_enum: bool) -> TypeInfo:
    types = dialect_types(dialect)
    raw = data_type.strip()
    is_array = raw.endswith("[]")
    while raw.endswith("[]"):
        raw = raw[:-2].rstrip()
    return TypeInfo(
        base=base_type(raw),
        length=type_length(raw),
        types=types,
        is_array=is_array,
        is_enum=is_enum,
    )


def type_info(dialect: str | None, data_type: str, type_definition: str | None = None) -> TypeInfo:
    is_enum = bool(type_definition and type_definition.lstrip().lower().startswith("enum"))
    return _type_info((dialect or "mssql").lower(), data_type, is_enum)
