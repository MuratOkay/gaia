"""Golden drafting (7B, D97): where Gaia looks, and a context of chosen objects."""

from __future__ import annotations

from datetime import date

import yaml

from gaia.evaluation.drafting import Draft, DraftQuestion, choose_seeds, drafts_yaml
from gaia.query.retrieval import IndexedColumn, IndexedObject, context_of


def _index():
    from test_retrieval import _index as build

    index = build()
    # a table nothing joins to, whose only meaning is an identifier (its other columns are
    # empty or constant: set aside as unused) — nothing to ask about
    index.objects["legacy.xx_aux"] = IndexedObject("legacy", "xx_aux", "table", 5000, columns=[
        IndexedColumn("id", "integer", business_name="Row id", role="identifier"),
        IndexedColumn("a1", "varchar")])
    index.catalog.objects.setdefault("legacy", {})["xx_aux"] = {"id": "integer",
                                                                 "a1": "varchar"}
    return index


def test_seeds_are_the_connected_tables_one_schema_at_a_time():
    index = _index()
    seeds = choose_seeds(index, 10)
    heads = [s[0] for s in seeds]
    assert heads[0] == "sales.orders"  # three joins, the most rows
    assert seeds[0][1:] == ["sales.order_lines", "crm.customers", "store.stores"]
    # every schema before any second one; a table already inside a context is not a seed
    assert heads == ["sales.orders", "legacy.tb_trx02", "product.products", "hr.employees"]
    assert "legacy.xx_aux" not in heads
    assert choose_seeds(index, 2) == seeds[:2]
    # names only, nowhere a meaning: every table is worth a look
    for o in index.objects.values():
        o.business_name = None
        for c in o.columns:
            c.business_name = None
    assert "legacy.xx_aux" in [s[0] for s in choose_seeds(index, 10)]


def test_a_context_of_chosen_objects_holds_their_joins_only():
    index = _index()
    ctx = context_of(index, ["sales.orders", "store.stores", "nothing.here"], note="seed")
    assert ctx.tables == ["sales.orders", "store.stores"]
    assert [(e.from_table, e.to_table) for e in ctx.joins] == [("sales.orders", "store.stores")]
    text = ctx.render()
    assert "TABLE sales.orders" in text and "order_lines" not in text


def test_the_file_marks_every_question_a_draft():
    d = Draft(DraftQuestion(question="How many stores are there?", sql="SELECT count(*) AS n "
                            "FROM store.stores", objects=["store.stores"], kind="weird"),
              "store.stores", tables=["store.stores"], rows=1, columns=["n"], preview=[["40"]])
    bad = Draft(DraftQuestion(question="Broken?", sql="SELECT x FROM y"), "store.stores",
                status="refused", problem="y is not an object")
    text = drafts_yaml([d, bad], data_source="demo_mssql", as_of=date(2016, 12, 31),
                       model="m", build="b", cost={"calls": 1, "tokens": 10, "duplicates": 0})
    doc = yaml.safe_load(text)
    assert doc["as_of"] == "2016-12-31" and doc["data_source"] == "demo_mssql"
    first, second = doc["questions"]
    assert first["status"] == second["status"] == "draft"
    assert first["draft"] == {"kind": "other", "seed": "store.stores", "check": "ok", "rows": 1,
                              "columns": ["n"], "preview": [["40"]]}
    assert second["draft"]["check"] == "refused" and second["draft"]["problem"]
    assert "1 ran and returned rows; 0 returned none; 1 were refused or failed" in text
    assert "sql: |\n    SELECT count(*) AS n FROM store.stores\n" in text
