"""Dialect contracts.

Everything engine-specific sits behind these three protocols, so adding a new
target database (PostgreSQL, MySQL, ...) is a new package under
``gaia.dialects`` plus one registry entry — no changes to the pipeline.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MetadataReader(Protocol):
    """Reads catalog metadata from a target database."""

    dialect: str

    def read_snapshot(self) -> dict[str, Any]:
        """Return the canonical snapshot document (see OUTLINE.md §9)."""
        ...


@runtime_checkable
class Profiler(Protocol):
    """Collects statistics and samples for a table or column."""

    dialect: str

    def profile_table(self, schema: str, table: str) -> dict[str, Any]:
        ...

    def profile_column(self, schema: str, table: str, column: str) -> dict[str, Any]:
        ...


@runtime_checkable
class SqlGuard(Protocol):
    """Validates generated SQL before it is executed."""

    dialect: str

    def validate(self, sql: str) -> list[str]:
        """Return a list of violations; empty means the statement is safe."""
        ...
