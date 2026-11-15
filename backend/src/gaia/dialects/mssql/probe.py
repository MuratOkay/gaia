"""``make check-target`` for SQL Server targets.

Checks, in order:

1. Connection (through the hardened session: isolation, lock timeout,
   statement timeout).
2. Catalog visibility, ``MS_Description`` comments, DMV row counts.
3. ``catalog.select`` — ``SELECT`` on every user table and view (profiling needs it).
4. ``security.privileges`` — read-only **proven from permissions** (Phase 3.0,
   after the 3.P finding that a DDL attempt in one safe schema proves nothing):

   * not ``sysadmin``; not in ``db_owner``, ``db_datawriter``, ``db_ddladmin``,
     ``db_securityadmin``, ``db_accessadmin``, ``db_backupoperator``;
   * every effective **database-level** permission (``fn_my_permissions``) is a
     read-type permission: ``CONNECT``, ``SELECT``, ``SHOWPLAN``, ``UNMASK`` or a
     ``VIEW …`` permission. Anything else is reported by name;
   * no ``INSERT``/``UPDATE``/``DELETE``/``ALTER``/``CONTROL``/``TAKE OWNERSHIP``
     on any user table or view (``HAS_PERMS_BY_NAME`` honours ``DENY``);
   * no ``ALTER`` on any schema (with ``CREATE TABLE`` it allows creating objects);
   * no ``EXECUTE`` on any procedure: through ownership chaining a procedure
     can modify data the caller cannot touch directly.

5. ``security.read_only`` — a ``CREATE TABLE`` inside a rolled-back transaction,
   aimed at a schema where the login holds ``ALTER`` if there is one (otherwise
   ``dbo``). Only a **permission** error (native 229, 230, 262, 297, 300, 2760,
   15247) counts as proof; any other error — syntax, timeout, a dropped
   connection — is reported as inconclusive, i.e. a failure. Until 3.0 any
   error counted as proof.
"""

from __future__ import annotations

import pyodbc

from gaia.core.config import TargetConfig
from gaia.dialects.errors import TargetError
from gaia.dialects.mssql.connection import GuardedCursor, native_error, session
from gaia.dialects.probe import Check

__all__ = ["PERMISSION_ERRORS", "READ_PERMISSIONS", "run_checks"]

# Native errors that mean "you are not allowed", as opposed to "something broke".
PERMISSION_ERRORS = frozenset({229, 230, 262, 297, 300, 2760, 15247})

# Effective database-level permissions a read-only login may hold. Every
# "VIEW ..." permission is also allowed (SQL Server 2022+ splits VIEW into many).
READ_PERMISSIONS = frozenset({"CONNECT", "SELECT", "SHOWPLAN", "UNMASK"})

WRITE_ROLES = (
    "db_owner",
    "db_datawriter",
    "db_ddladmin",
    "db_securityadmin",
    "db_accessadmin",
    "db_backupoperator",
)

_USER_OBJECTS = "o.is_ms_shipped = 0 AND o.type IN ('U', 'V')"
_QUALIFIED = "QUOTENAME(SCHEMA_NAME(o.schema_id)) + '.' + QUOTENAME(o.name)"
# User schemas: excludes sys, INFORMATION_SCHEMA, guest and the fixed-role schemas.
_USER_SCHEMAS = "s.schema_id < 16384 AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')"

PRIVILEGE_QUERIES = {
    "sysadmin": "SELECT IS_SRVROLEMEMBER('sysadmin')",
    "roles": "SELECT " + ", ".join(f"IS_ROLEMEMBER('{r}')" for r in WRITE_ROLES),
    "db_permissions": (
        "SELECT DISTINCT permission_name FROM fn_my_permissions(NULL, 'DATABASE') "
        "ORDER BY permission_name"
    ),
    "object_writes": f"""
        SELECT COUNT(*) FROM sys.objects o
        WHERE {_USER_OBJECTS}
          AND (   HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'INSERT') = 1
               OR HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'UPDATE') = 1
               OR HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'DELETE') = 1
               OR HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'ALTER') = 1
               OR HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'CONTROL') = 1
               OR HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'TAKE OWNERSHIP') = 1)
        """,  # noqa: S608 (module constants only)
    "schema_alter": f"""
        SELECT STRING_AGG(s.name, ', ') WITHIN GROUP (ORDER BY s.name)
        FROM sys.schemas s
        WHERE {_USER_SCHEMAS} AND HAS_PERMS_BY_NAME(QUOTENAME(s.name), 'SCHEMA', 'ALTER') = 1
        """,  # noqa: S608 (module constants only)
    "procedure_execute": f"""
        SELECT COUNT(*) FROM sys.objects o
        WHERE o.is_ms_shipped = 0 AND o.type IN ('P', 'PC')
          AND HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'EXECUTE') = 1
        """,  # noqa: S608 (module constants only)
    "unreadable": f"""
        SELECT COUNT(*) FROM sys.objects o
        WHERE {_USER_OBJECTS} AND HAS_PERMS_BY_NAME({_QUALIFIED}, 'OBJECT', 'SELECT') = 0
        """,  # noqa: S608 (module constants only)
    "probe_schema": f"""
        SELECT TOP (1) s.name FROM sys.schemas s
        WHERE {_USER_SCHEMAS}
        ORDER BY HAS_PERMS_BY_NAME(QUOTENAME(s.name), 'SCHEMA', 'ALTER') DESC,
                 CASE WHEN s.name = 'dbo' THEN 0 ELSE 1 END, s.name
        """,  # noqa: S608 (module constants only)
}


def _scalar(cur: GuardedCursor, sql: str) -> object:
    cur.execute(sql)
    row = cur.fetchone()
    return None if row is None else row[0]


def run_checks(target: TargetConfig, summary: bool = False) -> list[Check]:
    checks: list[Check] = []
    try:
        with session(target, autocommit=True) as cur:
            checks.append(Check("connect", True, f"connected to {target.host}:{target.port}"))
            checks.extend(_catalog_checks(cur))
            checks.append(_privilege_check(cur))
            checks.append(_ddl_probe(cur))
            if summary:
                _print_summary(cur)
    except (pyodbc.Error, TargetError) as exc:
        if not checks:
            checks.append(Check("connect", False, f"{type(exc).__name__}: {exc}"))
        else:
            checks.append(Check("probe", False, f"aborted: {type(exc).__name__}: {exc}"))
    return checks


def _catalog_checks(cur: GuardedCursor) -> list[Check]:
    checks: list[Check] = []
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
    checks.append(Check("catalog.read", True, f"tables={tables} views={views} columns={columns}"))

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
            "SELECT SUM(row_count) FROM sys.dm_db_partition_stats WHERE index_id IN (0, 1)",
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

    unreadable = _scalar(cur, PRIVILEGE_QUERIES["unreadable"])
    checks.append(
        Check(
            "catalog.select",
            unreadable == 0,
            "SELECT granted on every table and view"
            if unreadable == 0
            else f"{unreadable} table(s)/view(s) not readable; profiling needs SELECT",
        )
    )
    return checks


def disallowed_permissions(names: list[str]) -> list[str]:
    """Database-level permissions a read-only login must not hold."""
    return sorted(n for n in names if n not in READ_PERMISSIONS and not n.startswith("VIEW "))


def _privilege_check(cur: GuardedCursor) -> Check:
    problems: list[str] = []

    if _scalar(cur, PRIVILEGE_QUERIES["sysadmin"]) == 1:
        problems.append("login is sysadmin")

    cur.execute(PRIVILEGE_QUERIES["roles"])
    memberships = cur.fetchone() or ()
    roles = [r for r, member in zip(WRITE_ROLES, memberships, strict=False) if member == 1]
    if roles:
        problems.append("member of " + ", ".join(roles))

    cur.execute(PRIVILEGE_QUERIES["db_permissions"])
    names = [str(r[0]) for r in cur.fetchall()]
    extra = disallowed_permissions(names)
    if extra:
        problems.append("database permissions: " + ", ".join(extra))

    writable = _scalar(cur, PRIVILEGE_QUERIES["object_writes"])
    if writable:
        problems.append(f"write/alter permission on {writable} table(s)/view(s)")

    schemas = _scalar(cur, PRIVILEGE_QUERIES["schema_alter"])
    if schemas:
        problems.append(f"ALTER on schema(s): {schemas}")

    procedures = _scalar(cur, PRIVILEGE_QUERIES["procedure_execute"])
    if procedures:
        problems.append(
            f"EXECUTE on {procedures} procedure(s) (may write through ownership chaining)"
        )

    if problems:
        return Check("security.privileges", False, "; ".join(problems))
    return Check(
        "security.privileges",
        True,
        "read-type permissions only; no write, ALTER, EXECUTE or writer role",
    )


def _ddl_probe(cur: GuardedCursor) -> Check:
    schema = str(_scalar(cur, PRIVILEGE_QUERIES["probe_schema"]) or "dbo")
    target = f"[{schema.replace(']', ']]')}].[__gaia_write_probe]"
    try:
        cur.execute("BEGIN TRANSACTION")
        try:
            cur.execute(f"CREATE TABLE {target} (id int NULL)")
            return Check("security.read_only", False, f"DDL unexpectedly succeeded in {schema}")
        except pyodbc.Error as exc:
            native = native_error(exc)
            if native in PERMISSION_ERRORS:
                return Check(
                    "security.read_only",
                    True,
                    f"write denied as expected in schema {schema} (error {native})",
                )
            return Check(
                "security.read_only",
                False,
                f"inconclusive: DDL failed for a reason other than permissions "
                f"(error {native}): {exc}",
            )
        except TargetError as exc:
            return Check("security.read_only", False, f"inconclusive: {exc}")
        finally:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
    except pyodbc.Error as exc:
        return Check("security.read_only", False, f"probe inconclusive: {exc}")


def _print_summary(cur: GuardedCursor) -> None:
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
