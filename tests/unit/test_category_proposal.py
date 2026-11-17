"""Gaia's category proposals, version 2 (D77) — the owner's case first.

The owner made categories from ``demo_pg``'s schemas and let Gaia propose for
``nge_small``, whose tables all sit in ``dbo``: ``dbo.inventory`` became Product and
``dbo.inventory_movements`` Sales — one word in the second one's purpose decided it. Here a
``dbo`` schema shaped like ``nge_small`` (names from its catalog, meanings worded the way
the model words them) is proposed with the same categories."""

from __future__ import annotations

import uuid

from gaia.access.proposal import (
    CategoryTerms,
    ObjectFacts,
    literal_stems,
    propose_all,
    schema_keywords,
)

DEMO_PG_TABLES = {  # infra/pg-target/schema.sql, tables only
    "hr": ["departments", "employees"],
    "store": ["stores", "inventory"],
    "crm": ["customers", "addresses", "customer_notes"],
    "product": ["categories", "suppliers", "products", "price_history"],
    "sales": ["orders", "order_lines", "payments", "returns", "order_events"],
    "finance": ["currencies", "fx_rates", "invoices", "gl_entries"],
    "legacy": ["tb_mst01", "tb_trx02", "tb_prm03", "xx_aux"],
}


def _categories() -> dict[str, CategoryTerms]:
    """What "Create categories from the schemas" makes of demo_pg (plus Rpt, all views)."""
    words = schema_keywords(DEMO_PG_TABLES)
    out = {}
    for schema in [*DEMO_PG_TABLES, "rpt"]:
        name = schema.title()
        vocab = literal_stems(name) | literal_stems(schema)  # as CategoryService does
        for w in words.get(schema, []):
            vocab |= literal_stems(w)
        out[name] = CategoryTerms(uuid.uuid5(uuid.NAMESPACE_DNS, name), name, frozenset(vocab))
    return out


def _o(name, business_name, purpose, entity=None, refs=(), schema="dbo", **kw) -> ObjectFacts:
    return ObjectFacts(key=f"{schema}.{name}", schema=schema, name=name,
                       business_name=business_name, entity=entity, purpose=purpose,
                       references={f"dbo.{r}" if "." not in r else r for r in refs}, **kw)


def _nge_small() -> list[ObjectFacts]:
    return [
        _o("shops", "Shops", "The physical stores of the chain, with their city and size."),
        _o("platforms", "Gaming Platforms", "Consoles and PC platforms releases run on."),
        _o("hardware", "Hardware", "Consoles and accessories sold in the shops."),
        _o("releases", "Game Releases", "Each game released for a platform, with its price.",
           refs=["platforms"]),
        _o("inventory", "Inventory", "Current stock level of each release at each shop.",
           entity="stock of a product at a shop", refs=["releases", "shops"]),
        _o("inventory_movements", "Inventory Movements",
           "Every change of stock at a shop: sales, returns, trade-ins and transfers.",
           refs=["inventory", "shops"]),
        _o("customers", "Customers", "People who bought from the chain, with contact data."),
        _o("customer_addresses", "Customer Addresses", "Postal addresses of customers.",
           refs=["customers"]),
        _o("customer_emails", "Customer E-mails", "E-mail addresses of customers.",
           refs=["customers"]),
        _o("loyalty_memberships", "Loyalty Memberships",
           "A customer's membership of the loyalty programme and its points balance.",
           refs=["customers"]),
        _o("transactions", "Sales Transactions", "Each sale at a shop or online.",
           refs=["customers", "shops"]),
        _o("transaction_lines", "Transaction Lines",
           "The releases and hardware sold in a transaction, with quantity and price.",
           refs=["transactions", "releases", "hardware"]),
        _o("payments", "Payments", "How a transaction was paid.", refs=["transactions"]),
        _o("trade_ins", "Trade-ins", "Games customers traded in for store credit.",
           refs=["customers", "shops"]),
        _o("trade_in_items", "Trade-in Items", "The releases of a trade-in.",
           refs=["trade_ins", "releases"]),
        _o("currencies", "Currencies", "ISO currencies prices and payments are in."),
        _o("fx_rates", "Exchange Rates", "Daily exchange rates between currencies.",
           refs=["currencies"]),
        _o("source_systems", "Source Systems", "Systems the rows were loaded from."),
    ]


def _names(result, keys):
    return {k: (result[k].category.name if result[k].category else None) for k in keys}


def test_related_tables_are_proposed_together():
    cats = _categories()
    result = propose_all(_nge_small(), cats.values())
    got = _names(result, result)
    # the owner's report: the movements follow the inventory they are named after
    assert got["dbo.inventory_movements"] == got["dbo.inventory"] == "Store"
    assert result["dbo.inventory"].basis == "direct"  # "inventory" is a word of store's
    assert result["dbo.inventory_movements"].basis == "direct"
    # families: the lines with their transaction, the addresses and e-mails with customers
    assert got["dbo.transactions"] == got["dbo.transaction_lines"] == got["dbo.payments"] \
        == "Sales"
    assert got["dbo.customers"] == got["dbo.customer_addresses"] == \
        got["dbo.customer_emails"] == got["dbo.loyalty_memberships"] == "Crm"
    assert got["dbo.fx_rates"] == got["dbo.currencies"] == "Finance"
    assert got["dbo.trade_in_items"] == got["dbo.trade_ins"]
    # no category of the legacy schema on the strength of an expanded abbreviation
    assert "Legacy" not in got.values()
    # every proposal says why, in words
    assert "“inventory” in its name" in result["dbo.inventory_movements"].why
    assert "named after dbo.inventory" in result["dbo.inventory_movements"].why


def test_one_word_in_a_purpose_is_weak_and_does_not_beat_a_relative():
    cats = _categories()
    objs = [
        _o("stock", "Stock", "Current stock of each product at each shop.",
           entity="product stock"),
        _o("stock_moves", "Stock Moves",
           "Every change of stock: sales, returns and transfers.", refs=["stock"]),
    ]
    result = propose_all(objs, cats.values())
    # the head is weak (only its entity names a product) …
    assert result["dbo.stock"].category.name == "Product"
    assert result["dbo.stock"].basis == "weak" and "check it" in result["dbo.stock"].why
    # … yet its family follows it rather than a word ("sales") in its own purpose
    assert result["dbo.stock_moves"].category.name == "Product"
    assert result["dbo.stock_moves"].basis == "related"
    # alone, the purpose's word would have decided it — the behaviour the owner saw
    alone = propose_all([objs[1]], cats.values())
    assert alone["dbo.stock_moves"].category.name == "Sales"
    assert alone["dbo.stock_moves"].basis == "weak"


def test_a_confirmed_category_steers_its_neighbours():
    cats = _categories()
    objs = _nge_small()
    for o in objs:
        if o.name == "inventory":
            o.confirmed = cats["Product"].id  # an SME says: this is product data
    result = propose_all(objs, cats.values())
    assert result["dbo.inventory"].basis == "confirmed"
    # the movements are named after it and reference it: related evidence outweighs their
    # own word "inventory" (a Store keyword) — the family stays together
    assert result["dbo.inventory_movements"].category.name == "Product"
    assert result["dbo.inventory_movements"].basis == "related"


def test_schemas_still_decide_in_a_database_organised_by_them():
    cats = _categories()
    objs = [
        _o("customer_overview", "Customer Overview", "One row per customer with lifetime "
           "value.", schema="rpt", kind="view", reads={"crm.customers", "sales.orders"}),
        _o("customers", "Customers", "Customers.", schema="crm"),
        _o("orders", "Orders", "Orders.", schema="sales", refs=["crm.customers"]),
        _o("order_lines", "Order Lines", "Lines of an order, with the product.",
           schema="sales", refs=["sales.orders"]),
        _o("employees", "Employees", "Staff.", schema="hr"),
    ]
    result = propose_all(objs, cats.values())
    got = _names(result, result)
    assert got == {"rpt.customer_overview": "Rpt", "crm.customers": "Crm",
                   "sales.orders": "Sales", "sales.order_lines": "Sales", "hr.employees": "Hr"}
    # a view reading one category's objects gets it
    objs[0].reads = {"sales.orders", "sales.order_lines"}
    assert propose_all(objs, cats.values())["rpt.customer_overview"].basis == "view"


def test_nothing_or_a_tie_proposes_nothing():
    cats = _categories()
    result = propose_all([_o("source_systems", "Source Systems", "Where rows came from.")],
                         cats.values())
    assert result["dbo.source_systems"].category is None
    assert "no category" in result["dbo.source_systems"].why
    tie = propose_all([_o("customer_orders", "Customer Orders", "x")], cats.values())
    assert tie["dbo.customer_orders"].category is None
    assert tie["dbo.customer_orders"].why == "a tie between Crm and Sales"


def test_keywords_from_the_schemas():
    words = schema_keywords(DEMO_PG_TABLES)
    assert words["sales"] == ["order", "payment", "return"]
    assert words["crm"] == ["address", "customer"]
    assert words["store"] == ["inventory", "store"]
    assert words["legacy"] == []  # tb_trx02 is not evidence of "transactions"
    assert "price" in words["product"] and "history" not in words["product"]


def test_the_owners_confirmations_on_nge_small():
    """D79 — what the owner's D77 run showed (OWNER_VERIFICATION §27): his confirmations on
    ``nge_small`` against what v2 proposed beside them. Each case failed under v2."""
    cats = _categories()
    store, product, crm = cats["Store"].id, cats["Product"].id, cats["Crm"].id
    objs = [
        _o("countries", "Countries", "Countries of addresses.", confirmed=crm),
        _o("shops", "Shops", "The chain's stores.", confirmed=store),
        _o("platforms", "Platforms", "Gaming platforms.", confirmed=product),
        _o("releases", "Releases", "Game releases.", confirmed=product),
        _o("hardware", "Hardware", "Consoles.", confirmed=product),
        _o("customers", "Customers", "Customers.", confirmed=crm),
        _o("customer_addresses", "Customer Addresses", "Addresses.", refs=["customers"],
           confirmed=crm),
        _o("source_systems", "Source Systems", "Where rows came from."),
        # v2: Crm — "addresses" outweighed the schema hr
        _o("staff_addresses", "Staff Addresses", "Postal addresses of staff members.",
           entity="staff address", refs=["dbo.countries"], schema="hr"),
        # v2: Finance and Sales (weak) — a word of their descriptions
        _o("dim_shop", "Shop Dimension", "Shops with their reporting currency.", schema="dw"),
        _o("dim_platform", "Platform Dimension", "Platforms for sales reporting.", schema="dw"),
        # v2: Crm, and its lines Product — the lines' two product references outvoted it
        _o("transactions", "Transactions", "Each purchase of a customer, with the address.",
           refs=["customers", "customer_addresses", "source_systems"]),
        _o("transaction_lines", "Transaction Lines", "Items of a transaction.",
           refs=["transactions", "releases", "hardware"]),
        # not named after its header, but its key holds the reference: a part of it
        _o("basket_items", "Basket Items", "Items of a purchase.",
           refs=["transactions", "releases", "hardware"]),
    ]
    for o in objs:
        if o.name == "basket_items":
            o.identifying = {"dbo.transactions"}  # its key is (transaction_id, item_no)
    # confirmed Crm tables reference source_systems
    for o in objs:
        if o.name in ("customers", "customer_addresses"):
            o.references.add("dbo.source_systems")
    result = propose_all(objs, cats.values())
    got = _names(result, result)
    assert got["hr.staff_addresses"] == "Hr" and result["hr.staff_addresses"].basis == "direct"
    assert got["dw.dim_shop"] == "Store" and "named like dbo.shops" in result["dw.dim_shop"].why
    assert got["dw.dim_platform"] == "Product"
    assert got["dbo.transaction_lines"] == got["dbo.transactions"]
    assert got["dbo.basket_items"] == got["dbo.transactions"]
    assert "belongs to (its key holds the reference) dbo.transactions" in \
        result["dbo.basket_items"].why
    # a lookup table referenced by many: one small shared vote, marked for a look
    assert result["dbo.source_systems"].basis == "related"

    # what people confirmed in another data source counts for objects named like it
    elsewhere = [ObjectFacts(key="demo_pg:finance.gl_entries", schema="finance",
                             name="gl_entries", confirmed=cats["Finance"].id)]
    gl = _o("gl_entries", "General Ledger Entries", "Journal lines.")
    alone = propose_all([gl], cats.values())
    assert alone["dbo.gl_entries"].category is None
    helped = propose_all([gl], cats.values(), elsewhere)
    assert helped["dbo.gl_entries"].category.name == "Finance"
    assert "named like demo_pg:finance.gl_entries (confirmed there)" in \
        helped["dbo.gl_entries"].why
