"""Hardened PostgreSQL sessions for target databases.

Every session Gaia opens against a PostgreSQL target gets:

* a **read-only transaction** (``READ ONLY``), in addition to the role's own
  privileges — the privileges are the security boundary, this is the seatbelt;
* ``statement_timeout``, ``lock_timeout`` and
  ``idle_in_transaction_session_timeout`` from ``targets.<name>.limits``;
* ``search_path = pg_catalog``, so ``format_type``, ``pg_get_expr`` and
  ``pg_get_constraintdef`` always schema-qualify user objects. Without it the
  rendered text — and therefore every structure hash — would depend on the
  role's search path;
* ``REPEATABLE READ`` isolation for catalog reads, so a snapshot is captured
  against one consistent view. PostgreSQL never reads uncommitted data, so the
  SQL Server ``read_uncommitted`` setting has no equivalent and is ignored.

Settings are applied with ``SET`` after connecting rather than as startup
``options``, because connection poolers such as PgBouncer reject the latter.

``GuardedCursor`` translates ``statement_timeout`` cancellations
(``QueryCanceled``) into ``StatementTimeout`` and ``lock_timeout`` expiries
(``LockNotAvailable``) into ``LockTimeout``, matching the SQL Server dialect.

``autocommit=True`` runs every statement in its own read-only transaction
(``default_transaction_read_only``). Measurements use it: after a timeout
inside a transaction PostgreSQL aborts the whole transaction, so later
statements could not run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import errors

from gaia.core.config import TargetConfig
from gaia.dialects.errors import LockTimeout, StatementTimeout

__all__ = ["GuardedCursor", "connect_params", "session"]


def connect_params(target: TargetConfig) -> dict[str, object]:
    params: dict[str, object] = {
        "host": target.host,
        "port": target.port,
        "dbname": target.database,
        "user": target.username,
        "password": target.password,
        "connect_timeout": target.limits.connect_timeout_s,
        "application_name": target.options.app_name,
    }
    if target.options.sslmode:
        params["sslmode"] = target.options.sslmode
    return params


class GuardedCursor:
    """A psycopg cursor whose ``execute`` raises Gaia's timeout errors."""

    def __init__(self, cursor: psycopg.Cursor) -> None:
        self._cur = cursor

    def execute(self, query: Any, params: Any = None) -> GuardedCursor:
        try:
            if params is None:
                self._cur.execute(query)  # no placeholder parsing: LIKE '%' stays literal
            else:
                self._cur.execute(query, params)
        except errors.QueryCanceled as exc:
            if "statement timeout" not in str(exc):
                raise
            raise StatementTimeout(
                f"statement timed out: {exc}", dialect="postgres", statement=str(query)
            ) from exc
        except errors.LockNotAvailable as exc:
            raise LockTimeout(
                f"lock timeout: {exc}", dialect="postgres", statement=str(query)
            ) from exc
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def fetchmany(self, size: int) -> list[Any]:
        return self._cur.fetchmany(size)

    @property
    def description(self) -> Any:
        return self._cur.description


@contextmanager
def session(
    target: TargetConfig,
    *,
    isolation: str = "repeatable_read",
    read_only: bool = True,
    autocommit: bool = False,
    statement_timeout_s: int | None = None,
) -> Iterator[GuardedCursor]:
    """Yield a cursor of a hardened session; a transaction is always rolled back."""
    conn = psycopg.connect(**connect_params(target), autocommit=autocommit)
    try:
        if not autocommit:
            conn.isolation_level = (
                psycopg.IsolationLevel.REPEATABLE_READ
                if isolation == "repeatable_read"
                else psycopg.IsolationLevel.READ_COMMITTED
            )
            conn.read_only = read_only
        limits = target.limits
        timeout_s = (
            limits.statement_timeout_s if statement_timeout_s is None else statement_timeout_s
        )
        with conn.cursor() as raw:
            cur = GuardedCursor(raw)
            if autocommit and read_only:
                cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET search_path = pg_catalog")
            cur.execute(f"SET statement_timeout = {max(0, int(timeout_s)) * 1000}")
            cur.execute(f"SET lock_timeout = {int(limits.lock_timeout_ms)}")
            cur.execute(
                f"SET idle_in_transaction_session_timeout = {max(60, int(timeout_s) * 4) * 1000}"
            )
            yield cur
        if not autocommit:
            conn.rollback()
    finally:
        conn.close()
