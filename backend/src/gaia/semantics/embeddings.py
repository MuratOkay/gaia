"""Text that gets embedded for each table and column semantic version.

The text combines the physical name with the reasoned meaning, so retrieval
matches both "cust_id" and "customer". It is deterministic for a given
semantic version, and its sha256 is stored as ``content_hash`` on the
embedding row.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

__all__ = ["column_embedding_text", "content_hash", "table_embedding_text"]


def _join(items: Iterable[str]) -> str:
    return "; ".join(i for i in items if i)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def column_embedding_text(
    *,
    schema: str,
    table: str,
    column: str,
    business_name: str,
    purpose: str,
    role: str,
    entity: str | None,
    unit: str | None,
    synonyms: list[str],
    table_business_name: str | None,
    table_purpose: str | None,
) -> str:
    lines = [
        f"{schema}.{table}.{column}",
        f"{business_name}: {purpose}",
        _join(
            [
                f"Role: {role}",
                f"Entity: {entity}" if entity else "",
                f"Unit: {unit}" if unit else "",
            ]
        ),
    ]
    if synonyms:
        lines.append(f"Also called: {_join(synonyms)}")
    if table_business_name:
        lines.append(
            f"In table: {table_business_name}" + (f" - {table_purpose}" if table_purpose else "")
        )
    return "\n".join(line for line in lines if line)


def table_embedding_text(
    *,
    schema: str,
    table: str,
    business_name: str,
    purpose: str,
    grain: str | None,
    entity: str | None,
    synonyms: list[str],
    column_business_names: list[str],
) -> str:
    lines = [f"{schema}.{table}", f"{business_name}: {purpose}"]
    if grain:
        lines.append(f"Grain: {grain}")
    if entity:
        lines.append(f"Entity: {entity}")
    if synonyms:
        lines.append(f"Also called: {_join(synonyms)}")
    if column_business_names:
        lines.append(f"Columns: {_join(column_business_names)}")
    return "\n".join(lines)
