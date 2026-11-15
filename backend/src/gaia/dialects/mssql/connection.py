"""Hardened SQL Server sessions for target databases.

Every session Gaia opens against a SQL Server target gets:

* ``SET TRANSACTION ISOLATION LEVEL`` from ``limits.isolation``;
* ``SET LOCK_TIMEOUT`` from ``limits.lock_timeout_ms``;
* a **statement timeout**: the ODBC query timeout (``Connection.timeout``),
  from ``limits.statement_timeout_s`` unless the caller passes its own. It must
  be set before the cursor is created — pyodbc applies it to cursors created
  afterwards. Until Phase 3.0 this limit was configured but never applied;
* ``SET NOCOUNT ON``.

Exactly **one** cursor per session: pyodbc loses the active statement handle
when a second cursor is created while the first is unreferenced (a Phase 1
defect), so callers get a single ``GuardedCursor``.

``GuardedCursor`` translates driver errors: query timeout (SQLSTATE ``HYT00`` /
``HYT01``) → ``StatementTimeout``; lock timeout (native error 1222) →
``LockTimeout``. Everything else propagates unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyodbc

from gaia.core.config import TargetConfig
from gaia.dialects.errors import LockTimeout, StatementTimeout

__all__ = ["GuardedCursor", "native_error", "session"]

_TIMEOUT_STATES = {"HYT00", "HYT01"}
_NATIVE = re.compile(r"\((\d+)\)\s*(?:\(SQL\w+\))?\s*$")
_LOCK_TIMEOUT = 1222


def sqlstate(exc: BaseException) -> str | None:
    args = getattr(exc, "args", ())
    return str(args[0]) if args else None


def native_error(exc: BaseException) -> int | None:
    """SQL Server's native error number from a pyodbc message, e.g. ``(262)``."""
    args = getattr(exc, "args", ())
    text = str(args[1]) if len(args) > 1 else str(exc)
    # The ODBC driver may chain several diagnostics; the last number wins.
    found = None
    for line in text.splitlines():
        m = _NATIVE.search(line.strip())
        if m:
            found = int(m.group(1))
    return found


class GuardedCursor:
    """A pyodbc cursor whose ``execute`` raises Gaia's timeout errors."""

    def __init__(self, cursor: pyodbc.Cursor) -> None:
        self._cur = cursor

    def execute(self, sql: str, *params: Any) -> GuardedCursor:
        try:
            self._cur.execute(sql, *params)
        except pyodbc.Error as exc:
            if sqlstate(exc) in _TIMEOUT_STATES:
                raise StatementTimeout(
                    f"statement timed out: {exc}", dialect="mssql", statement=sql
                ) from exc
            if native_error(exc) == _LOCK_TIMEOUT:
                raise LockTimeout(f"lock timeout: {exc}", dialect="mssql", statement=sql) from exc
            raise
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def fetchmany(self, size: int) -> list[Any]:
        return self._cur.fetchmany(size)

    def nextset(self) -> bool | None:
        return self._cur.nextset()

    @property
    def messages(self) -> list[Any]:
        return list(getattr(self._cur, "messages", None) or [])

    @property
    def description(self) -> Any:
        return self._cur.description


@contextmanager
def session(
    target: TargetConfig,
    *,
    statement_timeout_s: int | None = None,
    autocommit: bool = False,
) -> Iterator[GuardedCursor]:
    """Yield the single cursor of a hardened session; uncommitted work is rolled back."""
    conn = pyodbc.connect(
        target.odbc_connection_string(),
        timeout=target.limits.connect_timeout_s,
        autocommit=autocommit,
    )
    try:
        limit = target.limits.statement_timeout_s
        timeout = limit if statement_timeout_s is None else statement_timeout_s
        # Before cursor(): pyodbc applies it to cursors created afterwards.
        conn.timeout = max(0, int(timeout))
        cur = GuardedCursor(conn.cursor())
        if target.limits.isolation == "read_uncommitted":
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        else:
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute(f"SET LOCK_TIMEOUT {int(target.limits.lock_timeout_ms)}")
        cur.execute("SET NOCOUNT ON")
        yield cur
        if not autocommit:
            conn.rollback()
    finally:
        conn.close()
