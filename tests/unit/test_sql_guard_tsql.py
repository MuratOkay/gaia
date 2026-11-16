"""The SQL Server (T-SQL) SQL guard (Phase 4A), offline.

D52 refinement (b): the guard's adversarial tests are blocking on both dialects
— they need no SQL Server. Live execution on SQL Server is asynchronous
(``integration/test_mssql_target_live.py``).
"""

from __future__ import annotations

import pytest
import sqlglot

from gaia.dialects.mssql.guard import MssqlGuard
from gaia.query.catalog import GuardCatalog, JoinEdge

OBJECTS = {
    "dbo": {
        "transactions": {"transaction_id": "bigint", "customer_id": "bigint",
                         "shop_id": "bigint", "total": "decimal(12,2)",
                         "occurred_at": "datetime2(3)", "currency_code": "char(3)"},
        "transaction_lines": {"transaction_line_id": "bigint", "transaction_id": "bigint",
                              "line_discount": "decimal(12,2)"},
        "customers": {"customer_id": "bigint", "date_of_birth": "date"},
        "shops": {"shop_id": "bigint", "name": "nvarchar(255)"},
    },
    "dw": {"dim_date": {"date_key": "int", "year": "int"},
           "fact_sales": {"date_key": "int", "line_total": "decimal(14,2)"}},
}
EDGES = [
    JoinEdge("dbo.transactions", "customer_id", "dbo.customers", "customer_id", "fk"),
    JoinEdge("dbo.transactions", "shop_id", "dbo.shops", "shop_id", "fk"),
    JoinEdge("dbo.transaction_lines", "transaction_id", "dbo.transactions", "transaction_id",
             "fk"),
    JoinEdge("dw.fact_sales", "date_key", "dw.dim_date", "date_key", "fk"),
]


UNIQUE = {"dbo.transactions.transaction_id", "dbo.transaction_lines.transaction_line_id",
          "dbo.customers.customer_id", "dbo.shops.shop_id", "dw.dim_date.date_key"}


@pytest.fixture()
def guard() -> MssqlGuard:
    return MssqlGuard(GuardCatalog("mssql", "nge_small", OBJECTS, EDGES, unique=UNIQUE),
                      row_limit=1000)


REFUSED = [
    ("DELETE FROM dbo.transactions", "statement_type"),
    ("UPDATE dbo.transactions SET total = 0", "statement_type"),
    ("INSERT INTO dbo.shops (shop_id) VALUES (1)", "statement_type"),
    ("MERGE dbo.shops AS t USING dbo.shops AS s ON t.shop_id = s.shop_id "
     "WHEN MATCHED THEN DELETE;", "statement_type"),
    ("TRUNCATE TABLE dbo.transactions", "statement_type"),
    ("DROP TABLE dbo.transactions", "statement_type"),
    ("CREATE TABLE dbo.x (a int)", "statement_type"),
    ("EXEC sp_who", "statement_type"),
    ("EXECUTE ('DELETE FROM dbo.transactions')", "statement_type"),
    ("EXEC xp_cmdshell 'dir'", "statement_type"),
    ("DECLARE @x int", "statement_type"),
    ("USE master", "statement_type"),
    ("SHUTDOWN", "statement_type"),
    ("BEGIN TRANSACTION", "statement_type"),
    ("WAITFOR DELAY '00:00:10'", "parse_error"),
    ("BULK INSERT dbo.shops FROM 'c:\\x.csv'", "parse_error"),
    ("SELECT 1; DELETE FROM dbo.transactions", "multiple_statements"),
    ("SELECT * INTO #copy FROM dbo.transactions", "select_into"),
    ("SELECT * INTO dbo.copy FROM dbo.transactions", "select_into"),
    ("SELECT * FROM #tmp", "temp_table"),
    ("SELECT * FROM srv.nge_small.dbo.transactions", "linked_server"),
    ("SELECT * FROM master.dbo.sysdatabases", "other_database"),
    ("SELECT * FROM sys.objects", "unknown_object"),
    ("SELECT * FROM sys.sql_logins", "unknown_object"),
    ("SELECT * FROM INFORMATION_SCHEMA.TABLES", "unknown_object"),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'Server=x;', 'SELECT 1')", "function_not_allowed"),
    ("SELECT * FROM OPENQUERY(srv, 'DELETE FROM t')", "function_not_allowed"),
    ("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=x').db.dbo.t",
     "function_not_allowed"),
    ("SELECT * FROM OPENJSON('[1]')", "function_not_allowed"),
    ("SELECT xp_cmdshell('dir')", "function_not_allowed"),
    ("SELECT NEXT VALUE FOR dbo.seq", "function_not_allowed"),
    ("SELECT SUSER_SNAME()", "function_not_allowed"),
    ("SELECT HAS_PERMS_BY_NAME('dbo.transactions', 'OBJECT', 'DELETE')", "function_not_allowed"),
    ("SELECT DB_NAME()", "function_not_allowed"),
    ("SELECT dbo.fn_secret(1)", "qualified_function"),
    ("SELECT * FROM dbo.fn_rows(1)", "qualified_function"),
    ("SELECT @@version", "variable"),
    ("SELECT total FROM dbo.transactions WHERE customer_id = @id", "variable"),
    ("SELECT * FROM dbo.transactions WITH (UPDLOCK)", "table_hint"),
    ("SELECT * FROM dbo.transactions WITH (TABLOCKX, HOLDLOCK)", "table_hint"),
    ("SELECT * FROM dbo.transactions OPTION (MAXDOP 64)", "query_option"),
    ("SELECT nope FROM dbo.transactions", "unknown_column"),
    ("SELECT * FROM dbo.transactions t, dbo.customers c WHERE t.customer_id = c.customer_id",
     "unverified_join"),
    ("SELECT * FROM dbo.transactions t JOIN dbo.shops s ON s.shop_id = t.customer_id",
     "unverified_join"),
    ("SELECT * FROM dbo.transactions t CROSS APPLY (SELECT TOP 1 * FROM dbo.customers c "
     "WHERE c.customer_id = t.customer_id) x", "unverified_join"),
    ("SELECT * FROM dbo.transactions a JOIN dbo.transactions b ON a.shop_id = b.shop_id",
     "unverified_join"),  # fan trap
]


@pytest.mark.parametrize(("sql", "code"), REFUSED)
def test_refused(guard, sql, code):
    result = guard.check(sql)
    assert not result.ok and result.sql is None, result.as_dict()
    assert code in {v.code for v in result.errors}, [str(v) for v in result.errors]


ACCEPTED = [
    ("SELECT COUNT_BIG(*) FROM dbo.transactions", ["dbo.transactions"], []),
    ("SELECT TOP 10 s.name, SUM(t.total) AS revenue FROM dbo.transactions t "
     "JOIN dbo.shops s ON s.shop_id = t.shop_id GROUP BY s.name ORDER BY revenue DESC",
     ["dbo.shops", "dbo.transactions"], ["fk"]),
    ("SELECT l.transaction_line_id FROM dbo.transaction_lines AS l WITH (NOLOCK) "
     "JOIN dbo.transactions t ON t.transaction_id = l.transaction_id",
     ["dbo.transaction_lines", "dbo.transactions"], ["fk"]),
    ("SELECT * FROM nge_small.dbo.shops", ["dbo.shops"], []),
    ("SELECT d.[year], SUM(f.line_total) FROM dw.fact_sales f JOIN dw.dim_date d "
     "ON d.date_key = f.date_key GROUP BY d.[year]", ["dw.dim_date", "dw.fact_sales"], ["fk"]),
    ("SELECT YEAR(occurred_at) AS y, DATEPART(month, occurred_at) AS m, "
     "CONVERT(varchar(7), occurred_at, 126) AS ym, ISNULL(total, 0), "
     "IIF(total > 100, 'big', 'small'), FORMAT(occurred_at, 'yyyy-MM'), "
     "DATEDIFF(day, occurred_at, SYSUTCDATETIME()) FROM dbo.transactions",
     ["dbo.transactions"], []),
    ("SELECT currency_code, STRING_AGG(CAST(transaction_id AS varchar(20)), ',') "
     "FROM dbo.transactions GROUP BY currency_code", ["dbo.transactions"], []),
    ("WITH x AS (SELECT customer_id, SUM(total) AS s FROM dbo.transactions GROUP BY customer_id) "
     "SELECT c.date_of_birth, x.s FROM x JOIN dbo.customers c ON c.customer_id = x.customer_id",
     ["dbo.customers", "dbo.transactions"], ["fk"]),
    ("SELECT 'EXEC xp_cmdshell ''dir''' AS harmless_text FROM dbo.shops", ["dbo.shops"], []),
    # a trailing word T-SQL would read as a second statement is only an alias once rendered
    ("SELECT * FROM dbo.shops SHUTDOWN", ["dbo.shops"], []),
]


@pytest.mark.parametrize(("sql", "tables", "joins"), ACCEPTED)
def test_accepted(guard, sql, tables, joins):
    result = guard.check(sql)
    assert result.ok, [str(v) for v in result.errors]
    assert result.tables == tables
    assert [j.kind for j in result.joins] == joins
    assert len([t for t in sqlglot.parse(result.sql, read="tsql") if t]) == 1


def test_rendered_text_is_what_executes(guard):
    result = guard.check("SELECT * FROM dbo.shops SHUTDOWN")
    assert "AS SHUTDOWN" in result.sql  # an alias, not the SHUTDOWN command


@pytest.mark.parametrize(("sql", "limit", "injected", "rendered"), [
    ("SELECT shop_id FROM dbo.shops", 1000, True, "TOP 1000"),
    ("SELECT TOP 10 shop_id FROM dbo.shops", 10, False, "TOP 10"),
    ("SELECT TOP (5000) shop_id FROM dbo.shops", 1000, True, "TOP 1000"),
    ("SELECT TOP (5) PERCENT shop_id FROM dbo.shops", 1000, True, "TOP 1000"),
    ("SELECT shop_id FROM dbo.shops ORDER BY shop_id OFFSET 10 ROWS FETCH NEXT 20 ROWS ONLY",
     20, False, "FETCH NEXT 20"),
    ("SELECT shop_id FROM dbo.shops ORDER BY shop_id OFFSET 0 ROWS FETCH NEXT 99999 ROWS ONLY",
     1000, True, "FETCH NEXT 1000"),
    ("SELECT shop_id FROM dbo.shops UNION SELECT shop_id FROM dbo.transactions",
     1000, True, "TOP 1000"),
])
def test_row_cap(guard, sql, limit, injected, rendered):
    result = guard.check(sql)
    if not result.ok and "PERCENT" in sql:
        return  # sqlglot < 26 cannot parse TOP … PERCENT: refused, which is safe
    assert result.ok, result.errors
    assert (result.row_limit, result.limit_injected) == (limit, injected)
    assert rendered in result.sql and "PERCENT" not in result.sql
