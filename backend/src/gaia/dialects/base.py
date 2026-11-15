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
    """Builds and runs the profiling statements of one dialect (Phase 3A).

    The orchestration (``gaia.profiling.statistics`` / ``service``) is dialect
    independent; a profiler supplies the SQL and runs it on a hardened,
    read-only session. Implementations: ``dialects/<d>/profiler.py``. Every
    statement is a ``SELECT``; a ``TOP``/``LIMIT`` only ever caps an ordered
    top-k or a ``TABLESAMPLE`` source, never acts as a sample itself.
    """

    dialect: str
    queries: int  # statements executed, for the zero-data-queries check

    def session(self) -> Any:
        """Context manager yielding a cursor (statement timeout, MAXDOP applied)."""
        ...

    def query(self, cur: Any, sql: str, params: Any = None, limit: int | None = None) -> list: ...

    def can_select(self, cur: Any, schema: str, name: str) -> bool: ...

    def scan_sql(self, t: str, columns: list[tuple[str, str]]) -> tuple[str, list]:
        """One batched scan: rows, non-null, min/max, lengths; with its result layout."""
        ...

    def sample_sql(
        self, t: str, exprs: list[str], rows: int, estimated: int | None, seed: int, whole: bool
    ) -> str: ...

    def topk_sql(self, t: str, name: str, limit: int) -> str: ...

    def inclusion_sql(
        self, parent: str, column: str, values: list, data_type: str | None = None
    ) -> tuple[str, Any]: ...

    def modules(self, cur: Any) -> list[tuple[str, str, str, str | None]]:
        """(schema, name, kind, definition) of views, procedures, functions, triggers."""
        ...


@runtime_checkable
class SqlGuard(Protocol):
    """Validates generated SQL before it is executed."""

    dialect: str

    def validate(self, sql: str) -> list[str]:
        """Return a list of violations; empty means the statement is safe."""
        ...
