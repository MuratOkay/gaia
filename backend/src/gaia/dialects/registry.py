"""Dialect registry.

Config refers to dialects by name (``targets.<name>.dialect``); this module maps
that name to concrete implementations.
"""

from __future__ import annotations

from collections.abc import Callable

from gaia.dialects.base import MetadataReader, Profiler, SqlGuard


class DialectRegistry:
    def __init__(self) -> None:
        self._readers: dict[str, Callable[..., MetadataReader]] = {}
        self._profilers: dict[str, Callable[..., Profiler]] = {}
        self._guards: dict[str, Callable[..., SqlGuard]] = {}

    def register_reader(self, dialect: str, factory: Callable[..., MetadataReader]) -> None:
        self._readers[dialect] = factory

    def register_profiler(self, dialect: str, factory: Callable[..., Profiler]) -> None:
        self._profilers[dialect] = factory

    def register_guard(self, dialect: str, factory: Callable[..., SqlGuard]) -> None:
        self._guards[dialect] = factory

    def reader(self, dialect: str, **kwargs) -> MetadataReader:
        return self._resolve(self._readers, dialect, "metadata reader")(**kwargs)

    def profiler(self, dialect: str, **kwargs) -> Profiler:
        return self._resolve(self._profilers, dialect, "profiler")(**kwargs)

    def guard(self, dialect: str, **kwargs) -> SqlGuard:
        return self._resolve(self._guards, dialect, "SQL guard")(**kwargs)

    @staticmethod
    def _resolve(registry: dict[str, Callable], dialect: str, kind: str) -> Callable:
        try:
            return registry[dialect]
        except KeyError:
            known = ", ".join(sorted(registry)) or "<none registered>"
            raise LookupError(f"no {kind} for dialect {dialect!r}; available: {known}") from None

    @property
    def dialects(self) -> list[str]:
        return sorted(set(self._readers) | set(self._profilers) | set(self._guards))


registry = DialectRegistry()
