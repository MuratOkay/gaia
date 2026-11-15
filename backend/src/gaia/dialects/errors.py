"""Dialect-neutral errors raised by target sessions.

Each dialect translates its driver's errors into these, so callers (the
catalog reader, the probe, the profiler's per-metric degradation in 3A, the
measurement harness) can tell a *timeout* — an expected, budgeted outcome —
from a real failure without knowing the driver.
"""

from __future__ import annotations

__all__ = ["LockTimeout", "StatementTimeout", "TargetError"]


class TargetError(RuntimeError):
    """A statement against a target database failed."""

    def __init__(self, message: str, *, dialect: str, statement: str | None = None) -> None:
        super().__init__(message)
        self.dialect = dialect
        self.statement = _shorten(statement)


class StatementTimeout(TargetError):
    """The statement ran longer than the session's statement timeout and was cancelled."""


class LockTimeout(TargetError):
    """The statement waited longer than the session's lock timeout."""


def _shorten(sql: str | None, limit: int = 300) -> str | None:
    if sql is None:
        return None
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
