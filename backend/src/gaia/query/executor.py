"""Run a guarded statement on a target, read-only (Phase 4A).

Only a ``GuardResult`` that passed is accepted, and only its rendered SQL is
executed — never the text a model or a user wrote (``query.validator``). The
session is the dialect's hardened one:

* PostgreSQL: a ``READ ONLY`` transaction (rolled back), ``READ COMMITTED``,
  ``statement_timeout`` / ``lock_timeout`` from ``targets.<name>.limits``,
  ``search_path = pg_catalog``;
* SQL Server: the ODBC query timeout, ``LOCK_TIMEOUT``, the configured isolation
  (``READ UNCOMMITTED`` by default), rolled back.

Rows are capped twice: by the guard's ``LIMIT`` / ``TOP`` and here, by fetching
at most ``row_limit + 1`` rows — so ``row_cap_reached`` is known exactly and
``WITH TIES`` cannot exceed the cap. Errors are classified for the repair loop
(Phase 4C): ``timeout``, ``lock_timeout``, ``error`` (the database's message).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from gaia.core.config import TargetConfig
from gaia.dialects.errors import LockTimeout, StatementTimeout
from gaia.query.validator import GuardResult

__all__ = ["ExecutionResult", "NotGuarded", "execute"]


class NotGuarded(ValueError):
    """A statement that did not pass the guard was handed to the executor."""


@dataclass
class ExecutionResult:
    status: str  # ok | timeout | lock_timeout | error
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_cap_reached: bool = False
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _session(target: TargetConfig, timeout_s: int | None):
    if target.dialect in ("postgres", "postgresql"):
        from gaia.dialects.postgres.connection import session

        return session(target, isolation="read_committed", read_only=True,
                       statement_timeout_s=timeout_s)
    if target.dialect == "mssql":
        from gaia.dialects.mssql.connection import session

        return session(target, statement_timeout_s=timeout_s)
    raise LookupError(f"no executor for dialect {target.dialect!r}")


def execute(target: TargetConfig, guarded: GuardResult, *, row_limit: int,
            statement_timeout_s: int | None = None) -> ExecutionResult:
    """Execute ``guarded.sql``; never raises for database errors (they are the result)."""
    if not isinstance(guarded, GuardResult) or not guarded.ok or not guarded.sql:
        raise NotGuarded("only a statement that passed the SQL guard can be executed")
    started = time.perf_counter()
    result = ExecutionResult(status="ok")
    try:
        with _session(target, statement_timeout_s) as cur:
            cur.execute(guarded.sql)
            result.columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(row_limit + 1) if cur.description else []
            # The cap was reached when more rows came than allowed (WITH TIES), or when
            # the guard's own cap (not the question's LIMIT) filled up: there may be more.
            result.row_cap_reached = len(rows) > row_limit or (
                guarded.limit_injected and len(rows) >= (guarded.row_limit or row_limit))
            result.rows = [tuple(r) for r in rows[:row_limit]]
    except StatementTimeout as exc:
        result.status, result.error = "timeout", str(exc).splitlines()[0][:500]
    except LockTimeout as exc:
        result.status, result.error = "lock_timeout", str(exc).splitlines()[0][:500]
    except Exception as exc:  # the database's own message, for the repair loop
        result.status = "error"
        result.error = f"{type(exc).__name__}: {str(exc).strip()}"[:1000]
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result
