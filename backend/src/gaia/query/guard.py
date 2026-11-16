"""Build the SQL guard of a target (Phase 4A)."""

from __future__ import annotations

from gaia.core.config import Settings
from gaia.dialects import bootstrap  # noqa: F401  (registers the built-in dialects)
from gaia.dialects.registry import registry
from gaia.query.catalog import GuardCatalog
from gaia.query.validator import SqlGuardBase

__all__ = ["build_guard", "row_limit"]


def row_limit(settings: Settings, target: str) -> int:
    """Rows returned at most: ``query.row_limit``, never above the target's own limit."""
    return max(1, min(settings.query.row_limit,
                      settings.target(target).limits.max_rows_returned))


def build_guard(settings: Settings, target: str, catalog: GuardCatalog) -> SqlGuardBase:
    dialect = settings.target(target).dialect
    return registry.guard(dialect, catalog=catalog, row_limit=row_limit(settings, target),
                          unverified_joins=settings.query.unverified_joins)
