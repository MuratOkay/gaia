"""A scripted stand-in for ``pyodbc`` so SQL Server code can be unit-tested
without a driver or a server.

``install()`` makes ``import pyodbc`` succeed when the real driver (or its
``libodbc``) is unavailable; tests then patch the ``pyodbc`` attribute of the
module under test with ``FakePyodbc(handler)``, whose ``connect`` returns a
``FakeConnection`` that answers each statement through ``handler(sql)``.
A handler returns a list of row tuples, or raises.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable


class Error(Exception):
    pass


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


def sql_error(native: int, message: str, state: str = "42000") -> ProgrammingError:
    driver = "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
    text = f"[{state}] {driver}{message} ({native}) (SQLExecDirectW)"
    return ProgrammingError(state, text)


def timeout_error() -> OperationalError:
    return OperationalError(
        "HYT00",
        "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Query timeout expired (0) "
        "(SQLExecDirectW)",
    )


def install() -> None:
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        module = types.ModuleType("pyodbc")
        for name, value in {
            "Error": Error, "DatabaseError": DatabaseError,
            "OperationalError": OperationalError, "ProgrammingError": ProgrammingError,
            "Cursor": object, "Connection": object,
        }.items():
            setattr(module, name, value)
        module.connect = lambda *a, **k: (_ for _ in ()).throw(Error("no driver"))
        sys.modules["pyodbc"] = module


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.rows: list[tuple] = []
        self.messages: list[tuple] = []

    def execute(self, sql: str, *params):
        self.conn.executed.append(sql)
        self.rows = list(self.conn.handler(sql) or [])
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def nextset(self):
        return False

    description = None


class FakeConnection:
    def __init__(self, handler: Callable[[str], list[tuple] | None], autocommit: bool) -> None:
        self.handler = handler
        self.autocommit = autocommit
        self.timeout = 0
        self.timeout_at_cursor: list[int] = []
        self.executed: list[str] = []
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        self.timeout_at_cursor.append(self.timeout)
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakePyodbc:
    Error = Error
    DatabaseError = DatabaseError
    OperationalError = OperationalError
    ProgrammingError = ProgrammingError
    Cursor = FakeCursor

    def __init__(self, handler: Callable[[str], list[tuple] | None]) -> None:
        self.handler = handler
        self.connections: list[FakeConnection] = []

    def connect(self, connection_string: str, timeout: int = 0, autocommit: bool = False):
        conn = FakeConnection(self.handler, autocommit)
        self.connections.append(conn)
        return conn
