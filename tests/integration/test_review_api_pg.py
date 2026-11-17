"""Phase 5B over HTTP, on the real application: only an SME of a data source reviews it,
every refusal is audited, and decisions are signed with the SME's own login (D68, D73).

The accounts (see ``_fake_directory``): akaya — SME of ``demo_pg``; e*star — SME of another
data source only; dyilmaz — administrator (never an SME); bdemir — auditor.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from test_auth_api_pg import PASSWORDS, _client, _login, api  # noqa: F401  (fixture)
from test_review_pg import _col, _world

from gaia.db.models.audit import AuthEvent
from gaia.db.models.catalog import DataSource

pytestmark = pytest.mark.skipif(not os.environ.get("GAIA_TEST_PG_TARGET_URL"),
                                reason="GAIA_TEST_PG_TARGET_URL not set")


@pytest.mark.asyncio
async def test_only_the_sme_of_a_data_source_reviews_it(api, fake_llm, fake_embedder):  # noqa: F811
    app, auth, factory = api
    async with factory() as s:
        s.add(DataSource(name="other_ds", dialect="postgres", database_name="other"))
        await s.commit()
    await auth.grant("sme", actor="setup", account=("acme", "e*star"), data_source="other_ds")
    _settings, review = await _world(factory, fake_llm, fake_embedder)
    app.state.review = review
    status = await _col(factory, "finance.invoices", "status")
    column = f"/api/review/demo_pg/columns/{status.id}"

    for login, code in (("dyilmaz", 403), ("bdemir", 403), ("e*star", 200)):
        async with await _client(app) as c:
            await _login(c, login, PASSWORDS[login])
            assert (await c.get("/api/review")).status_code == code, login
            assert (await c.get("/api/review/demo_pg/queue")).status_code == 403, login
            assert (await c.get(column)).status_code == 403, login
            r = await c.post(f"{column}/decision", json={"action": "accept"},
                             headers={"X-CSRF-Token": c.cookies.get("gaia_csrf")})
            assert r.status_code == 403, login
            if login == "e*star":
                mine = (await c.get("/api/review")).json()
                assert [d["name"] for d in mine] == ["other_ds"]
    async with factory() as s:
        refused = {(e.actor, e.subject) for e in (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "forbidden"))).all()}
    assert ("e*star", "GET /api/review/demo_pg/queue") in refused
    assert ("dyilmaz", "GET /api/review") in refused

    async with await _client(app) as ayse:
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        h = {"X-CSRF-Token": ayse.cookies.get("gaia_csrf")}
        mine = (await ayse.get("/api/review")).json()
        assert mine[0]["name"] == "demo_pg" and mine[0]["open"] > 0
        q = (await ayse.get("/api/review/demo_pg/queue?limit=5")).json()
        assert len(q["items"]) <= 5 and q["total"] >= len(q["items"])
        assert (await ayse.get("/api/review/demo_pg/queue?status=maybe")).status_code == 400
        assert (await ayse.get("/api/review/nowhere/queue")).status_code == 404
        item = (await ayse.get(column)).json()
        assert item["meaning"]["business_name"] == "Status"
        # changing needs the CSRF header; a bad decision is a 400 with the reason
        assert (await ayse.post(f"{column}/decision",
                                json={"action": "accept"})).status_code == 401
        r = await ayse.post(f"{column}/decision", json={"action": "reject"}, headers=h)
        assert r.status_code == 400 and "note" in r.json()["detail"]
        r = await ayse.post(f"{column}/decision", headers=h, json={
            "action": "correct", "note": "finance manual",
            "value_meanings": {"0": "open", "1": "paid", "2": "overdue", "9": "void"}})
        assert r.status_code == 200, r.text
        assert r.json()["meaning"]["reviewed_by"] == "akaya"  # the login, not the body
        r = await ayse.post("/api/review/demo_pg/glossary", headers=h, json={
            "term": "overdue invoice", "definition": "status 2 in finance.invoices"})
        assert r.status_code == 201
        terms = (await ayse.get("/api/review/demo_pg/glossary")).json()
        assert [t["term"] for t in terms] == ["overdue invoice"]
        listed = {t["name"]: t for t in (await ayse.get("/api/review/demo_pg/tables")).json()}
        assert listed["finance.invoices"]["decided"] == 1  # the decision above
        assert listed["finance.invoices"]["columns"] > 5 and "rpt.v_legacy_transactions" in listed
        assert sum(t["queued"] for t in listed.values()) == mine[0]["open"] - (
            1 if item["review"] and item["review"]["status"] == "open" else 0)
        table = (await ayse.get(f"/api/review/demo_pg/tables/{status.table_id}")).json()
        assert any(c["name"] == "status" for c in table["columns"])
        gone = "/api/review/demo_pg/columns/00000000-0000-0000-0000-000000000000"
        assert (await ayse.get(gone)).status_code == 404
    async with factory() as s:
        decided = (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "review_decision"))).all()
    assert [(e.actor, e.detail["action"]) for e in decided] == [("akaya", "correct")]
