"""SQL Server session hardening and the read-only probe (Phase 3.0).

Runs against a scripted fake driver; the live counterpart is
``tests/integration/test_mssql_target_live.py``.
"""

from __future__ import annotations

import _fake_pyodbc as fake
import pytest
import sqlglot

fake.install()

from gaia.core.config import TargetConfig, TargetLimits  # noqa: E402
from gaia.dialects.errors import LockTimeout, StatementTimeout  # noqa: E402
from gaia.dialects.mssql import connection as mconn  # noqa: E402
from gaia.dialects.mssql import probe as mprobe  # noqa: E402


def _target(**limits) -> TargetConfig:
    return TargetConfig(
        dialect="mssql", host="127.0.0.1", database="nge_small", username="gaia_ro",
        password="x", limits=TargetLimits(**limits),
    )


@pytest.fixture()
def driver(monkeypatch):
    def install(handler):
        drv = fake.FakePyodbc(handler)
        monkeypatch.setattr(mconn, "pyodbc", drv)
        monkeypatch.setattr(mprobe, "pyodbc", drv)
        return drv

    return install


# -- session ------------------------------------------------------------------------


def test_statement_timeout_is_set_before_the_cursor_exists(driver):
    drv = driver(lambda sql: [])
    with mconn.session(_target(statement_timeout_s=7)) as cur:
        cur.execute("SELECT 1")
    conn = drv.connections[0]
    assert conn.timeout_at_cursor == [7]
    assert conn.executed[:3] == [
        "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",
        "SET LOCK_TIMEOUT 5000",
        "SET NOCOUNT ON",
    ]
    assert conn.rolled_back and conn.closed


def test_explicit_statement_timeout_overrides_the_limit(driver):
    drv = driver(lambda sql: [])
    with mconn.session(_target(statement_timeout_s=30), statement_timeout_s=120, autocommit=True):
        pass
    assert drv.connections[0].timeout_at_cursor == [120]
    assert not drv.connections[0].rolled_back  # autocommit: nothing to roll back


def test_driver_timeouts_become_gaia_errors(driver):
    def handler(sql):
        if "WAITFOR" in sql:
            raise fake.timeout_error()
        if "UPDLOCK" in sql:
            raise fake.sql_error(1222, "Lock request time out period exceeded.")
        if "bogus" in sql:
            raise fake.sql_error(102, "Incorrect syntax near 'bogus'.")
        return []

    driver(handler)
    with mconn.session(_target()) as cur:
        with pytest.raises(StatementTimeout) as timed_out:
            cur.execute("WAITFOR DELAY '00:00:05'")
        assert timed_out.value.dialect == "mssql"
        assert "WAITFOR" in timed_out.value.statement
        with pytest.raises(LockTimeout):
            cur.execute("SELECT * FROM t WITH (UPDLOCK)")
        with pytest.raises(fake.Error):
            cur.execute("bogus")


def test_native_error_parsing():
    assert mconn.native_error(fake.sql_error(262, "CREATE TABLE permission denied")) == 262
    assert mconn.native_error(fake.timeout_error()) == 0
    assert mconn.native_error(fake.Error("HY000", "no number here")) is None


# -- probe ----------------------------------------------------------------------------

READ_ONLY_PERMISSIONS = [
    ("CONNECT",), ("SELECT",), ("VIEW DATABASE STATE",), ("VIEW DEFINITION",),
    ("VIEW ANY COLUMN MASTER KEY DEFINITION",), ("VIEW DATABASE PERFORMANCE STATE",),
]


def _scenario(**overrides):
    """Handler for a read-only login; ``overrides`` change single answers."""
    q = mprobe.PRIVILEGE_QUERIES
    answers = {
        q["sysadmin"]: [(0,)],
        q["roles"]: [tuple(0 for _ in mprobe.WRITE_ROLES)],
        q["db_permissions"]: READ_ONLY_PERMISSIONS,
        q["object_writes"]: [(0,)],
        q["schema_alter"]: [(None,)],
        q["procedure_execute"]: [(0,)],
        q["unreadable"]: [(0,)],
        q["probe_schema"]: [("dbo",)],
    }
    ddl = overrides.pop("ddl", lambda sql: (_ for _ in ()).throw(
        fake.sql_error(262, "CREATE TABLE permission denied in database 'nge_small'.")))
    answers.update({q[k]: v for k, v in overrides.items()})

    def handler(sql):
        if sql in answers:
            return answers[sql]
        if sql.startswith("CREATE TABLE"):
            return ddl(sql)
        if "SERVERPROPERTY" in sql:
            return [("17.0.4065.4",)]
        if "DB_NAME()" in sql:
            return [("nge_small",)]
        if "SUSER_SNAME" in sql:
            return [("gaia_ro",)]
        if "COUNT(*)" in sql or "SUM(row_count)" in sql:
            return [(0,)]
        return []

    return handler


def _checks(driver, handler) -> dict:
    drv = driver(handler)
    checks = {c.name: c for c in mprobe.run_checks(_target())}
    return checks, drv.connections[0]


def test_read_only_login_passes_every_check(driver):
    checks, conn = _checks(driver, _scenario())
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert len(checks) == 9
    assert "error 262" in checks["security.read_only"].detail
    assert conn.executed[-1] == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"
    assert conn.timeout_at_cursor == [30]  # the probe runs under the statement timeout too


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"sysadmin": [(1,)]}, "login is sysadmin"),
        ({"roles": [(0, 1, 0, 0, 0, 0)]}, "member of db_datawriter"),
        ({"db_permissions": [*READ_ONLY_PERMISSIONS, ("INSERT",), ("CREATE TABLE",)]},
         "database permissions: CREATE TABLE, INSERT"),
        ({"object_writes": [(1,)]}, "write/alter permission on 1 table(s)/view(s)"),
        ({"schema_alter": [("stage",)]}, "ALTER on schema(s): stage"),
        ({"procedure_execute": [(4,)]}, "EXECUTE on 4 procedure(s)"),
    ],
)
def test_any_write_path_fails_the_privilege_check(driver, override, expected):
    checks, _ = _checks(driver, _scenario(**override))
    assert not checks["security.privileges"].ok
    assert expected in checks["security.privileges"].detail


def test_ddl_attempt_aims_at_the_weakest_schema(driver):
    checks, conn = _checks(
        driver,
        _scenario(schema_alter=[("stage",)], probe_schema=[("stage",)], ddl=lambda sql: []),
    )
    assert "CREATE TABLE [stage].[__gaia_write_probe] (id int NULL)" in conn.executed
    assert not checks["security.read_only"].ok
    assert "unexpectedly succeeded in stage" in checks["security.read_only"].detail
    assert conn.executed[-1] == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"


@pytest.mark.parametrize(
    "error",
    [fake.sql_error(102, "Incorrect syntax near 'x'."), fake.timeout_error()],
    ids=["syntax-error", "timeout"],
)
def test_only_a_permission_error_counts_as_proof(driver, error):
    def ddl(sql):
        raise error

    checks, _ = _checks(driver, _scenario(ddl=ddl))
    assert not checks["security.read_only"].ok
    assert "inconclusive" in checks["security.read_only"].detail


def test_connection_failure_is_reported(driver, monkeypatch):
    drv = driver(lambda sql: [])
    monkeypatch.setattr(drv, "connect", lambda *a, **k: (_ for _ in ()).throw(
        fake.OperationalError("HYT00", "Login timeout expired")))
    checks = mprobe.run_checks(_target())
    assert [(c.name, c.ok) for c in checks] == [("connect", False)]


def test_disallowed_permissions():
    assert mprobe.disallowed_permissions(["CONNECT", "SELECT", "VIEW ANY DEFINITION"]) == []
    assert mprobe.disallowed_permissions(["EXECUTE", "SELECT", "ALTER"]) == ["ALTER", "EXECUTE"]


@pytest.mark.parametrize("name", sorted(mprobe.PRIVILEGE_QUERIES))
def test_probe_queries_are_valid_tsql(name):
    sqlglot.parse_one(mprobe.PRIVILEGE_QUERIES[name], read="tsql")
