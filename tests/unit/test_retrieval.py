"""Retrieval (Phase 4B) on a synthetic index: ranking, joins, columns, rendering,
and the property that matters most — every join the context offers passes the guard."""

from __future__ import annotations

import pytest

from gaia.dialects.postgres.guard import PostgresGuard
from gaia.query.catalog import GuardCatalog, JoinEdge
from gaia.query.retrieval import (
    IndexedColumn,
    IndexedObject,
    RetrievalIndex,
    RetrievalSettings,
    VectorHit,
    question_tokens,
    retrieve,
    tokens,
)


def C(name, dtype="text", bn=None, role=None, score=0.85, band="accepted", **kw):  # noqa: N802
    return IndexedColumn(name=name, data_type=dtype, business_name=bn, role=role,
                         score=score if bn else None, band=band if bn else None, **kw)


def _index() -> RetrievalIndex:
    objs = [
        IndexedObject("sales", "orders", "table", 250_000, "Customer Order",
                      "One sale to a customer.", "One row per order", synonyms=["sale"],
                      score=0.8, primary_key=["order_id"], columns=[
                          C("order_id", "bigint", "Order ID", "identifier", is_key=True),
                          C("customer_id", "bigint", "Customer", "reference"),
                          C("store_id", "smallint", "Store", "reference"),
                          C("channel", "sales.sales_channel", "Sales Channel", "code",
                            values=["store", "web", "mobile"]),
                          C("order_date", "date", "Order Date", "timestamp",
                            range=("2023-01-01", "2025-12-31")),
                          C("total_amount", "numeric(14,2)", "Order Total", "measure",
                            synonyms=["revenue", "sales amount"], unit="TRY"),
                      ]),
        IndexedObject("sales", "order_lines", "table", 1_000_000, "Order Line",
                      "A product on an order.", score=0.8, primary_key=["order_line_id"],
                      columns=[C("order_line_id", "bigint", "Order Line ID", "identifier"),
                               C("order_id", "bigint", "Order", "reference"),
                               C("product_id", "int", "Product", "reference"),
                               C("quantity", "int", "Quantity", "measure")]),
        IndexedObject("product", "products", "table", 3000, "Product", "Sellable item.",
                      score=0.8, primary_key=["product_id"],
                      columns=[C("product_id", "int", "Product ID", "identifier"),
                               C("sku", "varchar", "SKU", "code"),
                               C("name", "text", "Product Name", "dimension")]),
        IndexedObject("store", "stores", "table", 40, "Store", "A physical shop.", score=0.9,
                      primary_key=["store_id"],
                      columns=[C("store_id", "smallint", "Store ID", "identifier"),
                               C("city", "varchar", "City", "dimension",
                                 values=["Istanbul", "Ankara", "Izmir"], distinct=3)]),
        IndexedObject("crm", "customers", "table", 25_000, "Customer", "A buyer.", score=0.9,
                      primary_key=["customer_id"],
                      columns=[C("customer_id", "bigint", "Customer ID", "identifier"),
                               C("customer_code", "varchar", "Customer Code", "code"),
                               C("segment", "text", "Customer Segment", "dimension",
                                 score=0.55, band="deep_profile",
                                 values=["retail", "smb", "vip"], distinct=4),
                               C("email", "text", "E-mail", "dimension",
                                 value_note="personal data: values not shown")]),
        # the worst case: never reasoned, names only
        IndexedObject("legacy", "tb_trx02", "table", 120_000, columns=[
            IndexedColumn("f1", "bigint"), IndexedColumn("f2", "varchar"),
            IndexedColumn("f3", "integer", semantic_type="date_int"),
            IndexedColumn("f7", "numeric(14,2)")]),
        IndexedObject("hr", "employees", "table", 300, "Employee", "Staff member.",
                      score=0.4, columns=[C("employee_id", "int", "Employee ID", "identifier",
                                            score=0.4, band="deep_profile_flagged"),
                                          C("last_name", "text", "Last Name", "dimension")]),
    ]
    objects = {o.key: o for o in objs}
    edges = [
        JoinEdge("sales.orders", "customer_id", "crm.customers", "customer_id", "fk"),
        JoinEdge("sales.orders", "store_id", "store.stores", "store_id", "fk"),
        JoinEdge("sales.order_lines", "order_id", "sales.orders", "order_id", "fk"),
        JoinEdge("sales.order_lines", "product_id", "product.products", "product_id", "fk"),
        JoinEdge("legacy.tb_trx02", "f2", "crm.customers", "customer_code", "hint"),
    ]
    catalog = GuardCatalog(
        "postgres", "demo",
        {o.schema: {} for o in objs} | {
            s: {o.name: {c.name: c.data_type for c in o.columns} for o in objs
                if o.schema == s} for s in {o.schema for o in objs}},
        edges,
        unique={"sales.orders.order_id", "sales.order_lines.order_line_id",
                "product.products.product_id", "store.stores.store_id",
                "crm.customers.customer_id", "crm.customers.customer_code"})
    return RetrievalIndex("demo_pg", "postgres", "demo", objects, catalog)


def test_tokens():
    assert tokens("cust_qty_amt") == ["customer", "quantity", "amount"]
    assert tokens("OrderLines") == ["order", "line"]
    assert tokens("tb_trx02") == ["tb", "transaction"]
    assert tokens("the number of orders per store") == ["order", "store"]
    assert question_tokens("revenue last month by city") == ["revenue", "amount", "city"]
    assert question_tokens("orders in the past 30 days") == ["order"]
    # the owner's wording (P4-1): business words reach the database's words
    assert question_tokens("Top 5 shops performing best last year.") == [
        "shop", "store", "perform", "best"]
    assert question_tokens("top twenty most valuable customers") == [
        "valuable", "amount", "value", "customer"]
    assert question_tokens("ten dates where the days are higher") == ["date", "day"]
    assert question_tokens("which sales representative sold the most") == [
        "sale", "order", "amount", "representative", "employee", "sold"]


def test_meanings_and_synonyms_rank_the_right_objects():
    ctx = retrieve(_index(), "revenue by store city")
    assert ctx.ranking[0][0] in ("sales.orders", "store.stores")
    assert {"sales.orders", "store.stores"} <= set(ctx.tables)
    assert ctx.joins[0].kind == "fk"
    assert any(m.channel == "lexical" and "total_amount" in m.detail
               for r in ctx.objects if r.obj.key == "sales.orders" for m in r.matches)


def test_values_find_the_filter_column():
    ctx = retrieve(_index(), "how many web orders from Istanbul")
    matched = {m.detail.split(" ")[0] for r in ctx.objects for m in r.matches
               if m.channel == "value"}
    assert {"channel", "city"} <= matched


def test_an_object_without_meanings_is_found_by_its_names():
    ctx = retrieve(_index(), "trx amounts by date")  # trx -> transaction
    assert "legacy.tb_trx02" in ctx.tables
    text = ctx.render()
    assert "(no meaning yet: only its names are known)" in text
    assert "to_date(f3::text, 'YYYYMMDD')" in text  # the semantic type still helps


def test_bridge_objects_connect_what_was_found():
    # two objects found; the path between them runs through two others
    ctx = retrieve(_index(), "product name by customer segment",
                   settings=RetrievalSettings(max_objects=2))
    found = [r.obj.key for r in ctx.objects if not r.bridge]
    bridges = [r.obj.key for r in ctx.objects if r.bridge]
    assert sorted(found) == ["crm.customers", "product.products"]
    assert sorted(bridges) == ["sales.order_lines", "sales.orders"]
    pairs = {(e.from_table, e.to_table) for e in ctx.joins}
    assert pairs == {("sales.order_lines", "product.products"),
                     ("sales.order_lines", "sales.orders"), ("sales.orders", "crm.customers")}
    assert all(m.channel == "join" for r in ctx.objects if r.bridge for m in r.matches)


def test_every_join_offered_passes_the_guard():
    index = _index()
    guard = PostgresGuard(index.catalog, row_limit=10)
    for question in ("revenue by store city", "quantity by product and customer segment",
                     "legacy transaction amount by customer segment"):
        ctx = retrieve(index, question)
        assert ctx.joins, question
        for e in ctx.joins:
            sql = (f"SELECT count(*) FROM {e.from_table} a JOIN {e.to_table} b "  # noqa: S608
                   f"ON a.{e.from_column} = b.{e.to_column}")
            result = guard.check(sql)
            assert result.ok, (question, sql, [str(v) for v in result.errors])


def test_confidence_weights_the_ranking():
    index = _index()
    ctx = retrieve(index, "last name employee")
    base = dict(ctx.ranking)["hr.employees"]
    for c in index.objects["hr.employees"].columns:
        c.score, c.band = 0.95, "accepted"
    index.objects["hr.employees"].score = 0.95
    index._lex_columns = index._lex_objects = None
    better = dict(retrieve(index, "last name employee").ranking)["hr.employees"]
    assert better > base  # R2: ranked by score, not filtered by it


def test_vector_hits_add_what_words_miss():
    index = _index()
    without = retrieve(index, "which goods sell best")
    assert "product.products" not in without.tables[:1]
    hits = [VectorHit("product.products", None, 0.71),
            VectorHit("product.products", "name", 0.69),
            VectorHit("sales.order_lines", "quantity", 0.66)]
    with_vectors = retrieve(index, "which goods sell best", vector_hits=hits)
    assert with_vectors.ranking[0][0] in ("product.products", "sales.order_lines")
    assert any(m.channel == "vector" for r in with_vectors.objects for m in r.matches)
    assert "vector search not used: lexical ranking only" in without.notes


def test_uncertainty_is_disclosed():
    ctx = retrieve(_index(), "customers by segment")
    text = ctx.render()
    assert "segment text — Customer Segment [dimension] (uncertain 0.55)" in text
    assert "crm.customers.segment: Customer Segment (deep_profile, 0.55)" in ctx.uncertain
    assert "State any assumption" in text
    assert "personal data: values not shown" in text


def test_values_say_when_they_are_only_the_most_frequent():
    text = retrieve(_index(), "customers by segment").render()
    assert "most frequent values (3 of ~4): 'retail', 'smb', 'vip'" in text
    text = retrieve(_index(), "stores by city").render()
    assert "values: 'Istanbul', 'Ankara', 'Izmir'" in text


def test_wide_objects_show_keys_joins_and_matches(monkeypatch):
    index = _index()
    wide = index.objects["sales.orders"]
    wide.columns += [C(f"attr_{i:02d}", "text", f"Attribute {i}", "dimension")
                     for i in range(30)]
    index._lex_columns = index._lex_objects = None
    ctx = retrieve(index, "revenue by store city",
                   settings=RetrievalSettings(all_columns_up_to=10, max_columns_per_object=8))
    shown = next(r for r in ctx.objects if r.obj.key == "sales.orders").columns
    names = [c.name for c in shown]
    assert len(shown) <= 8 and {"order_id", "store_id", "total_amount"} <= set(names)
    assert "more column(s) not shown" in ctx.render()


def test_the_budget_trims_the_lowest_ranked_first():
    ctx = retrieve(_index(), "revenue by store city and customer segment",
                   settings=RetrievalSettings(context_budget_tokens=250))
    assert ctx.estimate_tokens() <= 400  # trimmed as far as it can go
    assert any("trimmed" in n for n in ctx.notes)
    assert ctx.objects[0].rank == 1


def test_unconnected_objects_are_noted():
    index = _index()
    ctx = retrieve(index, "employee last name and store city")
    assert any("no join path" in n for n in ctx.notes)


def test_nothing_matches():
    ctx = retrieve(_index(), "zzz qqq")
    assert ctx.objects == [] and any("nothing in the catalog matched" in n for n in ctx.notes)


def test_deterministic():
    a = retrieve(_index(), "revenue by store city").render()
    b = retrieve(_index(), "revenue by store city").render()
    assert a == b


@pytest.mark.parametrize(("dialect", "expected"), [
    ("postgres", "to_date(f3::text, 'YYYYMMDD')"),
    ("mssql", "CONVERT(date, CONVERT(char(8), f3), 112)"),
])
def test_semantic_type_hints_per_dialect(dialect, expected):
    index = _index()
    index.dialect = dialect
    assert expected in retrieve(index, "trx by date").render()


def test_implied_objects_for_rankings_and_periods():
    """The owner's "Who was the best supplier in 2025?" names neither the sales nor their
    dates. A ranking brings the facts that reference the best object; a period brings
    the dated parent of a fact without dates."""
    index = _index()
    ctx = retrieve(index, "Which product was best in 2025?",
                   settings=RetrievalSettings(max_objects=1))
    expansions = {r.obj.key: r.matches[0].detail for r in ctx.objects
                  if r.matches and r.matches[0].channel == "expansion"}
    assert expansions == {
        "sales.order_lines": "the activity behind a ranking of product.products",
        "sales.orders": "the dates of sales.order_lines"}
    pairs = {(e.from_table, e.to_table) for e in ctx.joins}
    assert {("sales.order_lines", "product.products"),
            ("sales.order_lines", "sales.orders")} <= pairs
    # no ranking, no period: nothing implied
    plain = retrieve(index, "product names", settings=RetrievalSettings(max_objects=1))
    assert not any(m.channel == "expansion" for r in plain.objects for m in r.matches)
    # switched off
    off = retrieve(index, "Which product was best in 2025?",
                   settings=RetrievalSettings(max_objects=1, max_expansions=0))
    assert set(off.tables) == {"product.products"}
