"""Dialect bootstrap: registers built-in implementations.

Imported by CLI tools and the API so ``registry.reader("mssql")`` works without
callers knowing which module implements it.

Registration is **lazy**: each factory imports its driver only when that
dialect is actually used. A PostgreSQL-only installation therefore never
imports ``pyodbc`` (and needs no ODBC driver), and vice versa.
"""

from __future__ import annotations

from importlib import import_module

from gaia.dialects.registry import registry

# dialect name -> package under gaia.dialects; aliases share a package
BUILTIN_DIALECTS = {
    "mssql": "mssql",
    "postgres": "postgres",
    "postgresql": "postgres",
}

_READERS = {"mssql": "MssqlCatalogReader", "postgres": "PostgresCatalogReader"}
_PROFILERS = {"mssql": "MssqlProfiler", "postgres": "PostgresProfiler"}
_GUARDS = {"mssql": "MssqlGuard", "postgres": "PostgresGuard"}


def _reader_factory(package: str):
    def factory(**kwargs):
        module = import_module(f"gaia.dialects.{package}.catalog")
        return getattr(module, _READERS[package])(**kwargs)

    return factory


def _profiler_factory(package: str):
    def factory(**kwargs):
        module = import_module(f"gaia.dialects.{package}.profiler")
        return getattr(module, _PROFILERS[package])(**kwargs)

    return factory


def _guard_factory(package: str):
    def factory(**kwargs):
        module = import_module(f"gaia.dialects.{package}.guard")
        return getattr(module, _GUARDS[package])(**kwargs)

    return factory


def _probe_loader(package: str):
    def loader():
        return import_module(f"gaia.dialects.{package}.probe").run_checks

    return loader


def register_builtin_dialects() -> None:
    """Idempotent registration of the dialects Gaia ships with."""
    for name, package in BUILTIN_DIALECTS.items():
        registry.register_reader(name, _reader_factory(package))
        registry.register_profiler(name, _profiler_factory(package))
        registry.register_guard(name, _guard_factory(package))
        registry.register_probe(name, _probe_loader(package))


register_builtin_dialects()
