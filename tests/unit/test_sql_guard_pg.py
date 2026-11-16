"""The PostgreSQL SQL guard (Phase 4A): the adversarial suite and what it lets through.

Phase 4 acceptance: "Zero statements outside SELECT/WITH reach execution,
including adversarial prompts (enforced by test, not by inspection)". Every
statement below that must be refused is refused with a specific code, and a
refused statement has no SQL to execute. Blocking for every delivery (D52).
"""

from __future__ import annotations

import pytest
import sqlglot

from gaia.dialects.postgres.guard import PostgresGuard
from gaia.query.catalog import GuardCatalog, JoinEdge
from gaia.query.executor import NotGuarded, execute

OBJECTS = {
    "sales": {
        "orders": {"order_id": "bigint", "customer_id": "bigint", "store_id": "smallint",
                   "order_date": "date", "status": "text", "total_amount": "numeric"},
        "order_lines": {"order_line_id": "bigint", "order_id": "bigint", "product_id": "int",
                        "quantity": "int", "unit_price": "numeric"},
        "payments": {"payment_id": "bigint", "order_id": "bigint", "amount": "numeric"},
    },
    "crm": {"customers": {"customer_id": "bigint", "customer_code": "varchar",
                          "first_name": "text", "segment": "text"}},
    "store": {"stores": {"store_id": "smallint", "store_code": "char", "city": "text"}},
    "legacy": {"tb_trx02": {"f1": "bigint", "f2": "varchar", "f7": "numeric", "f9": "char"}},
    "rpt": {"v_legacy_transactions": {"transaction_id": "bigint", "customer_code": "varchar",
                                      "amount": "numeric"}},
    "public": {"orders": {"id": "int"}},  # a second "orders": unqualified is ambiguous
}
EDGES = [
    JoinEdge("sales.orders", "customer_id", "crm.customers", "customer_id", "fk"),
    JoinEdge("sales.orders", "store_id", "store.stores", "store_id", "fk"),
    JoinEdge("sales.order_lines", "order_id", "sales.orders", "order_id", "fk"),
    JoinEdge("sales.payments", "order_id", "sales.orders", "order_id", "fk"),
    JoinEdge("legacy.tb_trx02", "f2", "crm.customers", "customer_code", "hint"),  # D48
    JoinEdge("rpt.v_legacy_transactions", "customer_code", "legacy.tb_trx02", "f2", "lineage"),
]


UNIQUE = {"sales.orders.order_id", "sales.order_lines.order_line_id",
          "sales.payments.payment_id", "crm.customers.customer_id",
          "crm.customers.customer_code", "store.stores.store_id", "store.stores.store_code",
          "public.orders.id"}


def _catalog(**kw) -> GuardCatalog:
    return GuardCatalog("postgres", "gaia_target_pg", OBJECTS, EDGES, unique=UNIQUE, **kw)


@pytest.fixture()
def guard() -> PostgresGuard:
    return PostgresGuard(_catalog(), row_limit=1000)


REFUSED = [
    # not a query
    ("DELETE FROM sales.orders", "statement_type"),
    ("UPDATE sales.orders SET status = 'x'", "statement_type"),
    ("INSERT INTO sales.orders (order_id) VALUES (1)", "statement_type"),
    ("TRUNCATE sales.orders", "statement_type"),
    ("DROP TABLE sales.orders", "statement_type"),
    ("CREATE TABLE x (a int)", "statement_type"),
    ("ALTER TABLE sales.orders ADD COLUMN x int", "statement_type"),
    ("GRANT SELECT ON sales.orders TO public", "statement_type"),
    ("COPY sales.orders TO '/tmp/orders.csv'", "statement_type"),
    ("COPY (SELECT 1) TO PROGRAM 'rm -rf /'", "statement_type"),
    ("DO $$ BEGIN DELETE FROM sales.orders; END $$", "statement_type"),
    ("CALL sales.refresh()", "statement_type"),
    ("SET statement_timeout = 0", "statement_type"),
    ("RESET ALL", "statement_type"),
    ("SET ROLE postgres", "statement_type"),
    ("LOCK TABLE sales.orders", ("statement_type", "parse_error")),  # by sqlglot version
    ("VACUUM sales.orders", "statement_type"),
    ("ANALYZE sales.orders", "statement_type"),
    ("EXPLAIN ANALYZE SELECT 1", "statement_type"),
    ("PREPARE p AS SELECT 1", "statement_type"),
    ("EXECUTE p", "statement_type"),
    ("LISTEN x", "statement_type"),
    ("NOTIFY x", "statement_type"),
    ("BEGIN", "statement_type"),
    ("COMMIT", "statement_type"),
    ("REFRESH MATERIALIZED VIEW rpt.mv_product_margin", "statement_type"),
    ("VALUES (1), (2)", "statement_type"),
    ("SHOW ALL", "statement_type"),
    # more than one statement, however hidden
    ("SELECT 1; DELETE FROM sales.orders", "multiple_statements"),
    ("SELECT 1;SELECT 2", "multiple_statements"),
    # writes and locks inside a query
    ("WITH d AS (DELETE FROM sales.orders RETURNING *) SELECT * FROM d", "write_operation"),
    ("WITH u AS (UPDATE sales.orders SET status = 'x' RETURNING order_id) SELECT * FROM u",
     "write_operation"),
    ("SELECT * INTO sales.copy FROM sales.orders", "select_into"),
    ("SELECT * FROM sales.orders FOR UPDATE", "locking_clause"),
    ("SELECT * FROM sales.orders FOR SHARE", "locking_clause"),
    # functions that undo the session's protection, write, lock, sleep or read files
    ("SELECT set_config('statement_timeout', '0', false)", "function_not_allowed"),
    ("SELECT current_setting('data_directory')", "function_not_allowed"),
    ("SELECT pg_sleep(600)", "function_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
    ("SELECT pg_ls_dir('.')", "function_not_allowed"),
    ("SELECT lo_import('/etc/passwd')", "function_not_allowed"),
    ("SELECT nextval('sales.orders_order_id_seq')", "function_not_allowed"),
    ("SELECT setval('sales.orders_order_id_seq', 1)", "function_not_allowed"),
    ("SELECT pg_advisory_lock(1)", "function_not_allowed"),
    ("SELECT pg_terminate_backend(1)", "function_not_allowed"),
    ("SELECT pg_cancel_backend(1)", "function_not_allowed"),
    ("SELECT pg_reload_conf()", "function_not_allowed"),
    ("SELECT txid_current()", "function_not_allowed"),
    ("SELECT query_to_xml('DELETE FROM sales.orders', true, true, '')", "function_not_allowed"),
    ("SELECT * FROM dblink('host=x', 'DELETE FROM t') AS t(a int)", "function_not_allowed"),
    ("SELECT dblink_exec('host=x', 'DROP TABLE t')", "function_not_allowed"),
    ("SELECT version()", "function_not_allowed"),
    ("SELECT order_id FROM sales.orders WHERE pg_sleep(1) IS NULL", "function_not_allowed"),
    ("SELECT count(*) FILTER (WHERE pg_sleep(1) IS NULL) FROM sales.orders",
     "function_not_allowed"),
    ("SELECT public.my_func(1)", "qualified_function"),
    ("SELECT pg_catalog.now()", "qualified_function"),
    ("SELECT * FROM public.generate_series(1, 3) AS g(x)", "qualified_function"),
    # objects outside the catalog: system catalogs, other schemas, unknown names
    ("SELECT * FROM pg_catalog.pg_authid", "unknown_object"),
    ("SELECT * FROM pg_shadow", "unknown_object"),
    ("SELECT * FROM information_schema.tables", "unknown_object"),
    ("SELECT * FROM sales.nothere", "unknown_object"),
    ("SELECT * FROM otherdb.sales.orders", "other_database"),
    ("SELECT * FROM orders", "ambiguous_object"),
    ("SELECT nope FROM sales.orders", "unknown_column"),
    ("SELECT o.nope FROM sales.orders o", "unknown_column"),
    # joins without a join path
    ("SELECT * FROM sales.orders o, crm.customers c WHERE o.customer_id = c.customer_id",
     "unverified_join"),
    ("SELECT * FROM sales.orders CROSS JOIN crm.customers", "unverified_join"),
    ("SELECT * FROM sales.orders NATURAL JOIN sales.payments", "unverified_join"),
    ("SELECT * FROM sales.orders o JOIN crm.customers c ON o.store_id = c.customer_id",
     "unverified_join"),
    ("SELECT * FROM sales.order_lines l JOIN crm.customers c ON l.quantity = c.customer_id",
     "unverified_join"),
    ("WITH x AS (SELECT amount::bigint AS k FROM sales.payments) "
     "SELECT * FROM sales.orders o JOIN x ON x.k = o.customer_id", "unverified_join"),
    ("SELECT * FROM sales.orders o JOIN crm.customers c ON true", "unverified_join"),
    # fan traps: a join path, but neither side unique — rows multiply
    ("SELECT l.order_line_id, p.payment_id FROM sales.order_lines l "
     "JOIN sales.payments p ON p.order_id = l.order_id", "unverified_join"),
    ("SELECT * FROM sales.orders a JOIN sales.orders b ON a.status = b.status",
     "unverified_join"),
    ("SELECT * FROM rpt.v_legacy_transactions v JOIN legacy.tb_trx02 t "
     "ON t.f2 = v.customer_code", "unverified_join"),
    # parse errors
    ("SELEC * FROM sales.orders", "parse_error"),
    ("", "empty"),
    ("   ;  ", "empty"),
]


@pytest.mark.parametrize(("sql", "code"), REFUSED)
def test_refused(guard, sql, code):
    result = guard.check(sql)
    assert not result.ok and result.sql is None, result.as_dict()
    codes = {code} if isinstance(code, str) else set(code)
    assert codes & {v.code for v in result.errors}, [str(v) for v in result.errors]


def test_a_refused_statement_cannot_be_executed(guard):
    result = guard.check("DELETE FROM sales.orders")
    with pytest.raises(NotGuarded):
        execute(None, result, row_limit=10)  # type: ignore[arg-type]
    with pytest.raises(NotGuarded):
        execute(None, "SELECT 1", row_limit=10)  # type: ignore[arg-type]


ACCEPTED = [
    # (sql, objects read, join kinds)
    ("SELECT count(*) FROM sales.orders", ["sales.orders"], []),
    ("select c.segment, sum(o.total_amount) from sales.orders o "
     "join crm.customers c on o.customer_id = c.customer_id group by c.segment",
     ["crm.customers", "sales.orders"], ["fk"]),
    ("SELECT first_name FROM customers", ["crm.customers"], []),  # unqualified, unique
    ("SELECT o.order_id, p.amount FROM sales.orders o JOIN sales.payments p "
     "USING (order_id)", ["sales.orders", "sales.payments"], ["fk"]),
    # two children of one key, one side aggregated by it first: no fan-out
    ("WITH l AS (SELECT order_id, sum(quantity) AS q FROM sales.order_lines GROUP BY order_id) "
     "SELECT p.payment_id, l.q FROM sales.payments p JOIN l ON l.order_id = p.order_id",
     ["sales.order_lines", "sales.payments"], ["shared_key"]),
    # a tested model hint (D48) and view lineage
    ("SELECT c.segment, sum(t.f7) FROM legacy.tb_trx02 t "
     "JOIN crm.customers c ON c.customer_code = t.f2 GROUP BY 1",
     ["crm.customers", "legacy.tb_trx02"], ["hint"]),
    ("SELECT c.segment, sum(v.amount) FROM rpt.v_legacy_transactions v "
     "JOIN crm.customers c ON c.customer_code = v.customer_code GROUP BY 1",
     ["crm.customers", "rpt.v_legacy_transactions"], ["hint_via_view"]),
    # through a CTE and a subquery
    ("WITH paid AS (SELECT order_id, sum(amount) AS paid FROM sales.payments GROUP BY order_id) "
     "SELECT o.order_id, o.total_amount - paid.paid AS open FROM sales.orders o "
     "JOIN paid ON paid.order_id = o.order_id", ["sales.orders", "sales.payments"], ["fk"]),
    ("SELECT s.city, x.n FROM store.stores s JOIN (SELECT store_id, count(*) AS n "
     "FROM sales.orders GROUP BY store_id) x ON x.store_id = s.store_id",
     ["sales.orders", "store.stores"], ["fk"]),
    # self-join on the same column; a date spine (generated)
    ("SELECT a.order_id FROM sales.orders a JOIN sales.orders b ON a.order_id = b.order_id",
     ["sales.orders"], ["same_column"]),
    ("SELECT d.day, count(o.order_id) FROM generate_series(DATE '2024-01-01', "
     "DATE '2024-01-31', INTERVAL '1 day') AS d(day) LEFT JOIN sales.orders o "
     "ON o.order_date = d.day GROUP BY d.day", ["sales.orders"], ["generated"]),
    # functions from the allowlist, windows, set operations, strings that look dangerous
    ("SELECT date_trunc('month', order_date) AS m, count(*), avg(total_amount), "
     "percentile_cont(0.5) WITHIN GROUP (ORDER BY total_amount) FROM sales.orders "
     "GROUP BY 1 ORDER BY 1", ["sales.orders"], []),
    ("SELECT order_id, rank() OVER (PARTITION BY customer_id ORDER BY total_amount DESC) "
     "FROM sales.orders", ["sales.orders"], []),
    ("SELECT customer_id FROM sales.orders UNION SELECT customer_id FROM crm.customers",
     ["crm.customers", "sales.orders"], []),
    ("SELECT 'DELETE FROM sales.orders; SELECT pg_sleep(9)' AS note, "
     "coalesce(status, 'n/a') FROM sales.orders", ["sales.orders"], []),
    ("SELECT to_char(order_date, 'YYYY-MM'), extract(year FROM order_date), "
     "order_date::text, CAST(total_amount AS int), now() - interval '1 day' "
     "FROM sales.orders WHERE status IN ('paid', 'shipped')", ["sales.orders"], []),
    ("(SELECT order_id FROM sales.orders)", ["sales.orders"], []),
    ("SELECT * FROM sales.orders;", ["sales.orders"], []),
    ("SELECT o.* FROM sales.orders o WHERE EXISTS (SELECT 1 FROM sales.payments p "
     "WHERE p.order_id = o.order_id)", ["sales.orders", "sales.payments"], []),
]


@pytest.mark.parametrize(("sql", "tables", "joins"), ACCEPTED)
def test_accepted(guard, sql, tables, joins):
    result = guard.check(sql)
    assert result.ok, [str(v) for v in result.errors]
    assert result.tables == tables
    assert [j.kind for j in result.joins] == joins
    # what executes is a single SELECT that parses as one statement
    assert len([t for t in sqlglot.parse(result.sql, read="postgres") if t]) == 1


def test_unqualified_names_are_qualified(guard):
    # the session runs with search_path = pg_catalog: every object must carry its schema
    result = guard.check("SELECT first_name FROM customers")
    assert "FROM crm.customers" in result.sql


@pytest.mark.parametrize(("sql", "limit", "injected"), [
    ("SELECT order_id FROM sales.orders", 1000, True),
    ("SELECT order_id FROM sales.orders LIMIT 10", 10, False),
    ("SELECT order_id FROM sales.orders LIMIT 5000", 1000, True),
    ("SELECT order_id FROM sales.orders LIMIT ALL", 1000, True),
    ("SELECT order_id FROM sales.orders LIMIT 10 OFFSET 20", 10, False),
    ("SELECT order_id FROM sales.orders FETCH FIRST 5 ROWS ONLY", 5, False),
    ("SELECT order_id FROM sales.orders FETCH FIRST 50 PERCENT ROWS ONLY", 1000, True),
    ("SELECT order_id FROM sales.orders FETCH FIRST 9000 ROWS ONLY", 1000, True),
    ("SELECT order_id FROM sales.orders UNION SELECT order_id FROM sales.payments", 1000, True),
])
def test_row_cap(guard, sql, limit, injected):
    result = guard.check(sql)
    if not result.ok and ("LIMIT ALL" in sql or "PERCENT" in sql):
        return  # sqlglot < 26 cannot parse these: refused, which is safe
    assert result.ok, result.errors
    assert (result.row_limit, result.limit_injected) == (limit, injected)
    outer = sqlglot.parse_one(result.sql, read="postgres")
    node = outer.args.get("limit")
    assert node is not None and str(limit) in node.sql("postgres")


def test_row_cap_of_a_subquery_is_not_the_statement_cap(guard):
    result = guard.check(
        "SELECT * FROM (SELECT order_id FROM sales.orders LIMIT 5) x")
    assert result.limit_injected and result.sql.rstrip().endswith("LIMIT 1000")


def test_fan_out_is_named_as_such(guard):
    result = guard.check("SELECT l.order_line_id, p.payment_id FROM sales.order_lines l "
                         "JOIN sales.payments p ON p.order_id = l.order_id")
    assert [j.kind for j in result.joins] == ["fan_out"]
    assert "multiplies rows" in result.errors[0].message


def test_unverified_joins_can_be_downgraded_to_warnings():
    g = PostgresGuard(_catalog(), row_limit=10, unverified_joins="warn")
    result = g.check("SELECT * FROM sales.orders o JOIN crm.customers c ON o.store_id = "
                     "c.customer_id")
    assert result.ok and [v.code for v in result.warnings] == ["unverified_join"]
    # everything else stays an error
    assert not g.check("SELECT pg_sleep(1)").ok


def test_catalog_key_classes():
    cat = _catalog()
    assert cat.join_kind("sales.orders.customer_id", "crm.customers.customer_id") == "fk"
    assert cat.join_kind("SALES.ORDERS.CUSTOMER_ID", "crm.customers.customer_id") == "fk"
    assert cat.join_kind("sales.payments.order_id", "sales.order_lines.order_id") == "shared_key"
    # lineage is an alias, not an edge: the view column is its base column
    assert cat.base("rpt.v_legacy_transactions.customer_code") == "legacy.tb_trx02.f2"
    assert cat.join_kind("rpt.v_legacy_transactions.customer_code",
                         "crm.customers.customer_code") == "hint_via_view"
    assert cat.join_kind("rpt.v_legacy_transactions.customer_code",
                         "legacy.tb_trx02.f2") == "same_column"
    assert cat.join_kind("sales.orders.store_id", "crm.customers.customer_id") is None
    assert cat.join_kind("x.y.z", "x.y.z") == "same_column"
    assert cat.is_unique("crm.customers.customer_code")
    assert not cat.is_unique("sales.order_lines.order_id")
    # for retrieval: the view gets the base column's join path
    via = [e for e in cat.join_edges() if e.kind == "via_view"]
    assert [(e.from_table, e.from_column, e.to_table, e.to_column) for e in via] == [
        ("rpt.v_legacy_transactions", "customer_code", "crm.customers", "customer_code")]
