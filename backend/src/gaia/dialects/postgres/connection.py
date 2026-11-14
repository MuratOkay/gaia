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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from gaia.core.config import TargetConfig

__all__ = ["connect_params", "session"]


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


@contextmanager
def session(
    target: TargetConfig, *, isolation: str = "repeatable_read", read_only: bool = True
) -> Iterator[psycopg.Cursor]:
    """Yield a cursor inside one hardened transaction; always rolled back."""
    conn = psycopg.connect(**connect_params(target), autocommit=False)
    try:
        conn.isolation_level = (
            psycopg.IsolationLevel.REPEATABLE_READ
            if isolation == "repeatable_read"
            else psycopg.IsolationLevel.READ_COMMITTED
        )
        conn.read_only = read_only
        limits = target.limits
        with conn.cursor() as cur:
            cur.execute("SET search_path = pg_catalog")
            cur.execute(f"SET statement_timeout = {int(limits.statement_timeout_s) * 1000}")
            cur.execute(f"SET lock_timeout = {int(limits.lock_timeout_ms)}")
            cur.execute(
                "SET idle_in_transaction_session_timeout = "
                f"{max(60, int(limits.statement_timeout_s) * 4) * 1000}"
            )
            yield cur
        conn.rollback()
    finally:
        conn.close()
