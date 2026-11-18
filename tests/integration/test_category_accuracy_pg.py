"""6G (D88): how often Gaia's category proposal is what people confirmed, on ``demo_pg`` —
today's rule with each confirmation hidden in turn, and the proposals people actually saw."""

from __future__ import annotations

import os
import uuid

import pytest

from gaia.access.categories import CategoryService

pytestmark = pytest.mark.skipif(not os.environ.get("GAIA_TEST_PG_TARGET_URL"),
                                reason="GAIA_TEST_PG_TARGET_URL not set")


@pytest.mark.asyncio
async def test_proposals_against_what_people_confirmed(pg_session_factory):
    from test_profiling_pg import _introspect

    factory = pg_session_factory
    await _introspect(factory)
    svc = CategoryService(factory)
    await svc.from_schemas("demo_pg", actor="admin")
    await svc.propose("demo_pg")
    objs = {o["name"]: o for o in await svc.objects("demo_pg")}

    async def confirm(name, category):
        await svc.assign("demo_pg", uuid.UUID(objs[name]["id"]), category, actor="ayse",
                         source="sme")

    await confirm("sales.orders", "Sales")           # as proposed
    await confirm("product.products", "Product")     # as proposed
    await confirm("crm.customers", "Crm")            # as proposed
    await confirm("finance.invoices", "Sales")       # not as proposed (Finance)
    await confirm("finance.invoices", "Finance")     # a second thought: not about a proposal
    await confirm("legacy.tb_trx02", "Sales")        # the legacy transactions

    r = await svc.accuracy("demo_pg")
    rule, seen = r["rule"], r["seen"]
    assert rule["n"] == 5
    # the proposals people saw: the first decision on each object against the proposal before it
    assert seen["n"] == 5 and seen["no_proposal"] == 0
    assert seen["kept"] == 3 and sum(n for _k, n in seen["by_basis"].values()) == 5
    # today's rule, each confirmation hidden in turn
    assert rule["agree"] + len(rule["misses"]) + rule["none"] == 5
    misses = {m["object"]: m for m in rule["misses"]}
    assert "legacy.tb_trx02" in misses and misses["legacy.tb_trx02"]["proposed"] == "Legacy"
    assert misses["legacy.tb_trx02"]["confirmed"] == "Sales" and misses["legacy.tb_trx02"]["why"]
    assert "finance.invoices" not in misses  # confirmed Finance in the end, as the rule says
    assert all(b in ("direct", "view", "related", "weak") for b in rule["by_basis"])
    # nothing was changed by measuring
    after = {o["name"]: o for o in await svc.objects("demo_pg")}
    assert after["sales.orders"]["category"]["status"] == "confirmed"
    assert after["finance.invoices"]["category"]["name"] == "Finance"
