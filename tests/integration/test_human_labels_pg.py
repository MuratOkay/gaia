"""Labels from people (Phase 6B, D83) on ``demo_pg``: SME decisions through the review service,
read back as verdicts on the machine meanings they judged."""

from __future__ import annotations

import os
import uuid

import pytest

from gaia.evaluation.human_labels import decision_labels, label_summary

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")


@pytest.mark.asyncio
async def test_decisions_become_labels(pg_session_factory, fake_llm, fake_embedder):
    from test_review_pg import AYSE, _col, _world

    factory = pg_session_factory
    _settings, review = await _world(factory, fake_llm, fake_embedder)

    async def decide(table, column, action, **kw):
        col = await _col(factory, table, column)
        await review.decide_column("demo_pg", col.id, action, AYSE, **kw)

    await decide("finance.invoices", "status", "correct", note="finance manual",
                 changes={"role": "code"}, value_meanings={"2": "overdue"})          # wrong
    await decide("sales.orders", "total_amount", "accept")                           # right …
    await decide("sales.orders", "total_amount", "correct",                         # … then wrong
                 changes={"purpose": "The order's amount including tax."})
    await decide("crm.customers", "first_name", "correct", changes={"personal": True})  # right
    await decide("product.products", "name", "correct",
                 changes={"business_name": "Product name"})                          # partly
    await decide("sales.orders", "coupon_code", "reject", note="it is a campaign id")  # wrong
    await decide("legacy.xx_aux", "a1", "unused")                                    # no label

    async with factory() as s:
        labels = await decision_labels(s, "demo_pg")
    got = {x.column: (x.verdict, x.action) for x in labels}
    assert got == {
        "finance.invoices.status": ("wrong", "correct"),
        "sales.orders.total_amount": ("wrong", "correct"),  # the second decision replaced
        "crm.customers.first_name": ("right", "correct"),
        "product.products.name": ("partly", "correct"),
        "sales.orders.coupon_code": ("wrong", "reject"),
    }
    status = next(x for x in labels if x.column == "finance.invoices.status")
    assert status.changed == ["role", "value_meanings"] and status.reviewer == "ayse"
    assert status.model == "fake-model" and status.machine_version == 1 and 0 < status.raw_score < 1
    summary = label_summary(labels)
    assert summary["counts"] == {"right": 1, "partly": 1, "wrong": 3} and summary["n"] == 5
    assert summary["models"]["fake-model"]["n"] == 5


@pytest.mark.asyncio
async def test_some_items_are_shown_without_gaias_certainty(pg_session_factory, fake_llm,
                                                            fake_embedder):
    """P6-5 (D83): a share of the review items hides Gaia's certainty — everywhere it would
    show — and the decision says it was made blind; a filter on why an item is queued never
    lists a blind one (its membership would tell)."""
    from test_review_pg import AYSE, _col, _world

    from gaia.semantics.sme import ReviewService, is_blind

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    every = ReviewService(settings.model_copy(update={"semantics": settings.semantics.model_copy(
        update={"review_blind_share": 1.0})}), factory)
    q = await every.queue("demo_pg", limit=500)
    assert q["items"] and all(i["blind"] and i["score"] is None and i["band"] is None
                              and i["reasons"] == ["blind"] for i in q["items"])
    status = await _col(factory, "finance.invoices", "status")
    detail = await every.column("demo_pg", status.id, AYSE)
    assert detail["blind"] and detail["meaning"]["score"] is None
    assert detail["meaning"]["band"] is None and detail["breakdown"] is None
    assert [x["key"] for x in detail["summary"]][:2] == ["reads_as", "blind"]
    assert "unsure" not in [x["key"] for x in detail["summary"]]
    assert all(v["score"] is None for v in detail["versions"])
    table = await every.table("demo_pg", uuid.UUID(detail["table"]["id"]))
    assert all(c["score"] is None for c in table["columns"])
    assert (await every.queue("demo_pg", reason="evidence_exhausted"))["total"] == 0
    await every.decide_column("demo_pg", status.id, "accept", AYSE)
    async with factory() as s:
        labels = await decision_labels(s, "demo_pg")
    assert [x.blind for x in labels] == [True]
    after = await every.column("demo_pg", status.id, AYSE)
    assert not after["blind"] and after["meaning"]["score"] == 1.0  # decided: nothing to hide

    # the share in SQL is the share in Python: the filter leaves out exactly the blind ones
    half = ReviewService(settings.model_copy(update={"semantics": settings.semantics.model_copy(
        update={"review_blind_share": 0.5})}), factory)
    none = ReviewService(settings, factory)
    for reason in ("evidence_exhausted", "abstain"):
        allq = await none.queue("demo_pg", reason=reason, limit=500)
        kept = await half.queue("demo_pg", reason=reason, limit=500)
        expected = [i["column_id"] for i in allq["items"] if not is_blind(i["column_id"], 0.5)]
        assert [i["column_id"] for i in kept["items"]] == expected
        assert kept["total"] == len(expected)
