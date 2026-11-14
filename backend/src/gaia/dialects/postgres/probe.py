"""``make check-target`` for PostgreSQL targets.

PostgreSQL privileges are additive; there is no DENY. A role is read-only when
it owns nothing, holds no write privilege, cannot create objects and has no
role attribute that bypasses privileges. This probe proves each of those from
the catalog, then attempts a DDL statement inside an explicit ``READ WRITE``
transaction. Only a *permission* error counts as proof: a
``read_only_sql_transaction`` error would merely show that the session's
``default_transaction_read_only`` seatbelt is on — which any session can turn
off — so it is reported as inconclusive, i.e. a failure.
"""

from __future__ import annotations

import psycopg
from psycopg import errors, sql

from gaia.core.config import TargetConfig
from gaia.dialects.postgres.connection import connect_params
from gaia.dialects.probe import Check

__all__ = ["run_checks"]

_USER_SCHEMAS = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'
"""


def _scalar(cur: psycopg.Cursor, query: str, params: tuple | None = None) -> object:
    # No params -> no placeholder parsing, so LIKE patterns may contain "%".
    cur.execute(query, params) if params else cur.execute(query)
    row = cur.fetchone()
    return None if row is None else row[0]


def run_checks(target: TargetConfig, summary: bool = False) -> list[Check]:
    checks: list[Check] = []
    try:
        conn = psycopg.connect(**connect_params(target), autocommit=True)
    except Exception as exc:
        checks.append(Check("connect", False, f"{type(exc).__name__}: {exc}"))
        return checks

    try:
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {int(target.limits.statement_timeout_s) * 1000}")
        cur.execute(f"SET lock_timeout = {int(target.limits.lock_timeout_ms)}")
        checks.append(Check("connect", True, f"connected to {target.host}:{target.port}"))

        version = _scalar(cur, "SELECT current_setting('server_version')")
        checks.append(Check("server.version", True, f"PostgreSQL {version}"))
        db = _scalar(cur, "SELECT current_database()")
        user = _scalar(cur, "SELECT current_user")
        checks.append(Check("server.context", True, f"database={db} role={user}"))

        cur.execute(
            f"""
            SELECT count(*) FILTER (WHERE c.relkind IN ('r', 'p')),
                   count(*) FILTER (WHERE c.relkind IN ('v', 'm')),
                   (SELECT count(*) FROM pg_attribute a
                    JOIN pg_class c2 ON c2.oid = a.attrelid
                    JOIN pg_namespace n2 ON n2.oid = c2.relnamespace
                    WHERE a.attnum > 0 AND NOT a.attisdropped
                      AND c2.relkind IN ('r', 'p', 'v', 'm') AND NOT c2.relispartition
                      AND n2.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n2.nspname NOT LIKE 'pg\\_toast%'),
                   count(*) FILTER (WHERE obj_description(c.oid, 'pg_class') IS NOT NULL)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition AND {_USER_SCHEMAS}
            """  # noqa: S608 (module constants only)
        )
        tables, views, columns, table_comments = cur.fetchone()
        checks.append(
            Check("catalog.read", True, f"tables={tables} views={views} columns={columns}")
        )
        column_comments = _scalar(
            cur,
            f"""
            SELECT count(*) FROM pg_description d
            JOIN pg_class c ON c.oid = d.objoid AND d.classoid = 'pg_class'::regclass
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE d.objsubid > 0 AND {_USER_SCHEMAS}
            """,  # noqa: S608 (module constants only)
        )
        checks.append(
            Check(
                "catalog.comments",
                True,
                f"table comments={table_comments} column comments={column_comments}",
            )
        )

        stats_rows = _scalar(cur, "SELECT count(*) FROM pg_stat_user_tables")
        checks.append(Check("catalog.stats", True, f"pg_stat_user_tables rows={stats_rows}"))

        unreadable = _scalar(
            cur,
            f"""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition AND {_USER_SCHEMAS}
              AND NOT has_table_privilege(c.oid, 'SELECT')
            """,  # noqa: S608 (module constants only)
        )
        checks.append(
            Check(
                "catalog.select",
                unreadable == 0,
                "SELECT granted on every table and view"
                if unreadable == 0
                else f"{unreadable} table(s)/view(s) not readable; profiling needs SELECT",
            )
        )

        checks.append(_privilege_check(cur))
        checks.append(_ddl_probe(conn))

        if summary:
            cur.execute(
                f"""
                SELECT n.nspname,
                       CASE WHEN c.relkind IN ('v', 'm') THEN 'view' ELSE 'table' END,
                       count(*)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition
                  AND {_USER_SCHEMAS}
                GROUP BY 1, 2 ORDER BY 1, 2
                """  # noqa: S608 (module constants only)
            )
            print("\nCatalog summary")
            print("-" * 50)
            for schema_name, kind, count in cur.fetchall():
                print(f"  {schema_name:<20} {kind:<20} {count}")
            print()
    finally:
        conn.close()
    return checks


def _privilege_check(cur: psycopg.Cursor) -> Check:
    """Read-only by privileges: nothing owned, no write grant, no CREATE."""
    problems: list[str] = []

    cur.execute(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication
        FROM pg_roles WHERE rolname = current_user
        """
    )
    attrs = cur.fetchone()
    for flag, label in zip(
        attrs, ("SUPERUSER", "CREATEROLE", "CREATEDB", "BYPASSRLS", "REPLICATION"), strict=True
    ):
        if flag:
            problems.append(f"role has {label}")

    if _scalar(
        cur,
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pg_write_all_data') "
        "AND pg_has_role(current_user, 'pg_write_all_data', 'MEMBER')",
    ):
        problems.append("member of pg_write_all_data")

    owned = _scalar(
        cur,
        f"""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_SCHEMAS} AND pg_has_role(current_user, c.relowner, 'USAGE')
        """,  # noqa: S608 (module constants only)
    )
    if owned:
        problems.append(f"owns (or inherits ownership of) {owned} relation(s)")

    writable = _scalar(
        cur,
        f"""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND {_USER_SCHEMAS}
          AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
        """,  # noqa: S608 (module constants only)
    )
    if writable:
        problems.append(f"write privilege on {writable} relation(s)")

    sequences = _scalar(
        cur,
        f"""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'S' AND {_USER_SCHEMAS}
          -- CASE guards evaluation order: AND operands may run in any order
          AND CASE WHEN c.relkind = 'S'
                   THEN has_sequence_privilege(c.oid, 'USAGE, UPDATE') END
        """,  # noqa: S608 (module constants only)
    )
    if sequences:
        problems.append(f"USAGE/UPDATE (nextval) on {sequences} sequence(s)")

    creatable = _scalar(
        cur,
        f"""
        SELECT string_agg(n.nspname, ', ' ORDER BY n.nspname) FROM pg_namespace n
        WHERE {_USER_SCHEMAS} AND has_schema_privilege(n.oid, 'CREATE')
        """,  # noqa: S608 (module constants only)
    )
    if creatable:
        problems.append(f"CREATE on schema(s): {creatable}")
    if _scalar(cur, "SELECT has_database_privilege(current_database(), 'CREATE')"):
        problems.append("CREATE on the database")

    if problems:
        return Check("security.privileges", False, "; ".join(problems))
    return Check(
        "security.privileges",
        True,
        "owns nothing; no write, CREATE or bypassing role attribute",
    )


def _ddl_probe(conn: psycopg.Connection) -> Check:
    """Attempt DDL in a READ WRITE transaction; only a permission error proves anything."""
    schema = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT n.nspname FROM pg_namespace n
                WHERE {_USER_SCHEMAS} AND has_schema_privilege(n.oid, 'USAGE')
                -- Aim at the weakest spot: a schema where CREATE is granted, if
                -- any; otherwise public. Trying only a safe schema would pass a
                -- role that can write elsewhere.
                ORDER BY has_schema_privilege(n.oid, 'CREATE') DESC,
                         (n.nspname = 'public') DESC, n.nspname
                LIMIT 1
                """  # noqa: S608 (module constants only)
            )
            row = cur.fetchone()
            schema = row[0] if row else "public"
        with conn.transaction(force_rollback=True), conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(
                sql.SQL("CREATE TABLE {}.{} (id int)").format(
                    sql.Identifier(schema), sql.Identifier("__gaia_write_probe")
                )
            )
            return Check("security.read_only", False, f"DDL unexpectedly succeeded in {schema}")
    except errors.InsufficientPrivilege as exc:
        return Check(
            "security.read_only",
            True,
            f"write denied as expected in schema {schema} (SQLSTATE {exc.sqlstate})",
        )
    except errors.ReadOnlySqlTransaction:
        return Check(
            "security.read_only",
            False,
            "inconclusive: only the read-only transaction flag stopped the DDL; "
            "privileges were not exercised",
        )
    except psycopg.Error as exc:
        return Check("security.read_only", False, f"probe inconclusive: {exc}")
