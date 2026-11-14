"""``make check-target`` for SQL Server targets.

Checks performed:
1. TCP/ODBC connection succeeds.
2. The login can read the catalog (``sys.objects``, ``sys.columns``).
3. ``MS_Description`` extended properties are readable.
4. DMV row-count access (``sys.dm_db_partition_stats``) is available.
5. The login is genuinely read-only: a DDL attempt inside a transaction must
   fail. The transaction is always rolled back.

Moved verbatim from ``tools/check_target.py`` when PostgreSQL targets were
added; behaviour is unchanged.
"""

from __future__ import annotations

import pyodbc

from gaia.core.config import TargetConfig
from gaia.dialects.probe import Check

__all__ = ["run_checks"]


def _scalar(cur: pyodbc.Cursor, sql: str) -> object:
    cur.execute(sql)
    row = cur.fetchone()
    return None if row is None else row[0]


def run_checks(target: TargetConfig, summary: bool = False) -> list[Check]:
    checks: list[Check] = []
    try:
        conn = pyodbc.connect(
            target.odbc_connection_string(),
            timeout=target.limits.connect_timeout_s,
            autocommit=False,
        )
    except Exception as exc:
        checks.append(Check("connect", False, f"{type(exc).__name__}: {exc}"))
        return checks

    try:
        cur = conn.cursor()
        cur.execute(f"SET LOCK_TIMEOUT {target.limits.lock_timeout_ms}")
        checks.append(Check("connect", True, f"connected to {target.host}:{target.port}"))

        version = _scalar(cur, "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50))")
        checks.append(Check("server.version", True, f"SQL Server {version}"))

        db = _scalar(cur, "SELECT DB_NAME()")
        user = _scalar(cur, "SELECT SUSER_SNAME()")
        checks.append(Check("server.context", True, f"database={db} login={user}"))

        tables = _scalar(cur, "SELECT COUNT(*) FROM sys.tables WHERE is_ms_shipped = 0")
        views = _scalar(cur, "SELECT COUNT(*) FROM sys.views WHERE is_ms_shipped = 0")
        columns = _scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V')
            """,
        )
        checks.append(
            Check("catalog.read", True, f"tables={tables} views={views} columns={columns}")
        )

        table_comments = _scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM sys.extended_properties ep
            JOIN sys.objects o ON o.object_id = ep.major_id
            WHERE ep.name = 'MS_Description' AND ep.minor_id = 0 AND o.is_ms_shipped = 0
            """,
        )
        column_comments = _scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM sys.extended_properties ep
            WHERE ep.name = 'MS_Description' AND ep.minor_id > 0
            """,
        )
        checks.append(
            Check(
                "catalog.comments",
                True,
                f"table comments={table_comments} column comments={column_comments}",
            )
        )

        try:
            dmv_rows = _scalar(
                cur,
                "SELECT TOP (1) SUM(row_count) FROM sys.dm_db_partition_stats "
                "WHERE index_id IN (0, 1)",
            )
            checks.append(Check("catalog.dmv_rowcounts", True, f"sum(row_count)={dmv_rows}"))
        except pyodbc.Error as exc:
            code = exc.args[0] if exc.args else exc
            checks.append(
                Check(
                    "catalog.dmv_rowcounts",
                    False,
                    f"DMV unavailable ({code}); grant VIEW DATABASE STATE for exact row counts",
                )
            )

        write_denied = False
        write_detail = ""
        try:
            cur.execute("BEGIN TRANSACTION")
            try:
                cur.execute("CREATE TABLE dbo.__gaia_write_probe (id int NULL)")
                write_detail = "DDL unexpectedly succeeded"
            except pyodbc.Error as exc:
                write_denied = True
                write_detail = f"write denied as expected (SQLSTATE {exc.args[0]})"
            finally:
                cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
        except pyodbc.Error as exc:
            write_detail = f"probe inconclusive: {exc}"
            try:
                cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            except pyodbc.Error:
                pass
        checks.append(Check("security.read_only", write_denied, write_detail))

        if summary:
            cur.execute(
                """
                SELECT s.name AS schema_name, o.type_desc, COUNT(*) AS object_count
                FROM sys.objects o
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V')
                GROUP BY s.name, o.type_desc
                ORDER BY s.name, o.type_desc
                """
            )
            print("\nCatalog summary")
            print("-" * 50)
            for schema_name, type_desc, count in cur.fetchall():
                print(f"  {schema_name:<20} {type_desc:<20} {count}")
            print()
    finally:
        conn.close()

    return checks
