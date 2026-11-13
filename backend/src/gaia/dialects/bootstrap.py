"""Dialect bootstrap: registers built-in implementations.

Imported by CLI tools and the API so ``registry.reader("mssql")`` works without
callers knowing which module implements it.
"""

from __future__ import annotations

from gaia.dialects.registry import registry


def register_builtin_dialects() -> None:
    """Idempotent registration of the dialects Gaia ships with."""
    from gaia.dialects.mssql.catalog import MssqlCatalogReader

    registry.register_reader("mssql", MssqlCatalogReader)


register_builtin_dialects()
