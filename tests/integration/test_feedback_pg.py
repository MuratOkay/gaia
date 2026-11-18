"""People's "Was this right?" (6D, D87; P6-4) over HTTP, on the real application.

dyilmaz (administrator, who may chat) asks; akaya is the SME of ``demo_pg``. A person gives
feedback only on an answer of their own; "right" is counted, "partly" and "wrong" wait for the
SME, who decides what the answer was and why — and only that decision is a label.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from test_auth_api_pg import PASSWORDS, _client, _login, api  # noqa: F401  (fixture)

from gaia.db.models.audit import AuthEvent
from gaia.db.models.catalog import DataSource, SnapshotRecord
from gaia.db.models.chat import Feedback, Message, QueryRun
from gaia.evaluation.calibrators import ANSWER_SCORING, answer_points


async def _answer(factory, conversation_id: str, seq: int, kind: str = "answer") -> str:
    async with factory() as s:
        ds = await s.scalar(sa.select(DataSource).where(DataSource.name == "demo_pg"))
        snap = SnapshotRecord(data_source_id=ds.id, structure_hash="s", data_hash="d",
                        captured_at=datetime.now(UTC), document={})
        s.add(snap)
        await s.flush()
        run = QueryRun(data_source_id=ds.id, snapshot_id=snap.id, origin="api",
                       question="Revenue by city?", dialect="postgres",
                       submitted_sql="SELECT 1", executed_sql="SELECT 1 AS x LIMIT 1000",
                       status="ok", model="deepseek-test")
        s.add(run)
        await s.flush()
        m = Message(conversation_id=conversation_id, seq=seq, role="assistant", kind=kind,
                    content="Istanbul sold the most.", query_run_id=run.id, payload={
                        "question": "Revenue by city?", "interpretation": "Revenue per city",
                        "assumptions": ["cancelled excluded"], "columns": ["city", "revenue"],
                        "rows": [["Istanbul", 10]], "row_count": 1,
                        "confidence": {"level": "medium", "score": 0.71, "reasons": []}})
        s.add(m)
        await s.commit()
        return str(m.id)


@pytest.mark.asyncio
async def test_was_this_right_from_the_person_to_the_sme(api):  # noqa: F811
    app, _auth, factory = api
    async with await _client(app) as deniz, await _client(app) as ayse:
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        hd = {"X-CSRF-Token": deniz.cookies.get("gaia_csrf")}
        ha = {"X-CSRF-Token": ayse.cookies.get("gaia_csrf")}
        cid = (await deniz.post("/api/conversations", json={"data_source": "demo_pg"},
                                headers=hd)).json()["id"]
        answer = await _answer(factory, cid, 2)
        ask_back = await _answer(factory, cid, 4, kind="clarification")
        url = f"/api/messages/{answer}/feedback"

        # only the owner, only an answer, only a known verdict, only with the CSRF header
        assert (await ayse.put(url, json={"verdict": "wrong"}, headers=ha)).status_code == 404
        assert (await deniz.put(url, json={"verdict": "wrong"})).status_code == 401
        assert (await deniz.put(f"/api/messages/{ask_back}/feedback", json={"verdict": "wrong"},
                                headers=hd)).status_code == 400
        assert (await deniz.put(url, json={"verdict": "maybe"}, headers=hd)).status_code == 400

        # "right" is counted, not reviewed
        r = await deniz.put(url, json={"verdict": "right"}, headers=hd)
        assert r.status_code == 200 and r.json()["status"] == "none"
        assert (await ayse.get("/api/review/demo_pg/feedback")).json()["total"] == 0
        # changed to wrong: the SME's item
        r = await deniz.put(url, json={"verdict": "wrong", "note": "Ankara is missing"},
                            headers=hd)
        assert r.json()["status"] == "open"
        mine = (await ayse.get("/api/review")).json()
        assert mine[0]["feedback_open"] == 1
        items = (await ayse.get("/api/review/demo_pg/feedback")).json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["note"] == "Ankara is missing" and item["given_by"] == "dyilmaz"
        assert item["question"] == "Revenue by city?" and item["sql"].startswith("SELECT 1")
        assert item["confidence"]["score"] == 0.71 and item["model"] == "deepseek-test"
        # the administrator is not an SME: no item for him
        assert (await deniz.get("/api/review/demo_pg/feedback")).status_code == 403

        decide = f"/api/review/demo_pg/feedback/{item['id']}/decision"
        r = await ayse.post(decide, json={"decision": "wrong"}, headers=ha)
        assert r.status_code == 400 and "why" in r.json()["detail"]
        r = await ayse.post(decide, json={"decision": "wrong", "cause": "meaning",
                                          "note": "store city, not customer city"}, headers=ha)
        assert r.status_code == 200 and r.json()["decided_by"] == "akaya"
        assert (await ayse.get("/api/review/demo_pg/feedback")).json()["total"] == 0
        done = (await ayse.get("/api/review/demo_pg/feedback?status=decided")).json()["items"]
        assert done[0]["decision"] == "wrong" and done[0]["cause"] == "meaning"

        # the person sees the decision under the answer; changing their mind does not undo it
        conv = (await deniz.get(f"/api/conversations/{cid}")).json()
        fb = next(m for m in conv["messages"] if m["id"] == answer)["feedback"]
        assert fb["verdict"] == "wrong" and fb["decision"] == "wrong" and fb["cause"] == "meaning"
        r = await deniz.put(url, json={"verdict": "right"}, headers=hd)
        assert r.json()["status"] == "decided" and r.json()["decision"] == "wrong"

    async with factory() as s:
        events = (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "feedback_decided"))).all()
        assert [(e.actor, e.detail["decision"], e.detail["cause"]) for e in events] == [
            ("akaya", "wrong", "meaning")]
        # the decision — not the person's doubt — is the label for answer calibration
        points = await answer_points(s)
        assert [(p.score, p.y) for p in points[("deepseek-test", ANSWER_SCORING)]] == [(0.71, 0.0)]
        assert (await s.scalar(sa.select(sa.func.count()).select_from(Feedback))) == 1
        assert (await s.scalar(sa.select(sa.func.count()).select_from(Message))) >= 2


@pytest.mark.asyncio
async def test_a_doubt_nobody_reviews_says_so(api, monkeypatch, capsys):  # noqa: F811
    """D94 (OWNER_VERIFICATION §34): finn's "Partly" and "No" never reached akaya. A doubt on a
    data source nobody reviews waits for no one — the person, the report and the operator are
    told; and ``make feedback`` shows every doubt with who reviews its data source."""
    import argparse
    import os

    from gaia.db.engine import dispose_engine
    from gaia.db.models.auth import Grant
    from gaia.evaluation.feedback import all_feedback, reviewers
    from gaia.tools import feedback as feedback_tool

    app, auth, factory = api
    _auth = auth
    async with await _client(app) as deniz:
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        hd = {"X-CSRF-Token": deniz.cookies.get("gaia_csrf")}
        cid = (await deniz.post("/api/conversations", json={"data_source": "demo_pg"},
                                headers=hd)).json()["id"]
        first, second = await _answer(factory, cid, 2), await _answer(factory, cid, 4)
        # D95 (OWNER_VERIFICATION §35): on the host dyilmaz — an administrator through
        # GAIA-Admins — also held an SME grant for demo_pg (the conflict of §25, on purpose) and
        # was listed as a reviewer; an administrator's SME grant is not applied (D68)
        await auth.grant("sme", actor="setup", account=("acme", "dyilmaz"),
                         data_source="demo_pg")
        async with factory() as s:
            ds = await s.scalar(sa.select(DataSource.id).where(DataSource.name == "demo_pg"))
            assert await reviewers(s, ds) == ["acme/akaya"]
        r = await deniz.put(f"/api/messages/{first}/feedback", json={"verdict": "partly"},
                            headers=hd)
        assert r.json()["status"] == "open" and r.json()["no_reviewer"] is False
        # the note may come later, on its own (D92)
        r = await deniz.put(f"/api/messages/{first}/feedback",
                            json={"verdict": "partly", "note": "only half the stores"}, headers=hd)
        assert r.json()["note"] == "only half the stores"

        async with factory() as s:  # akaya no longer reviews demo_pg
            await s.execute(sa.update(Grant).where(Grant.privilege == "sme").values(
                revoked_at=sa.func.now()))
            await s.commit()
            assert await reviewers(s, ds) == []
        # the administrator's SME grant alone: still nobody (it was "reviewed by: dyilmaz")
        await auth.grant("sme", actor="setup", account=("acme", "dyilmaz"),
                         data_source="demo_pg")
        # nor a directory role whose members who signed in are all administrators
        await auth.grant("sme", actor="setup", role=("acme", "GAIA-Admins"),
                         data_source="demo_pg")
        async with factory() as s:
            assert await reviewers(s, ds) == []
        r = await deniz.put(f"/api/messages/{second}/feedback", json={"verdict": "wrong"},
                            headers=hd)
        assert r.json()["no_reviewer"] is True
        conv = (await deniz.get(f"/api/conversations/{cid}")).json()
        assert [m["feedback"]["no_reviewer"] for m in conv["messages"]
                if m.get("feedback")] == [True, True]
        assert (await deniz.put(f"/api/messages/{first}/feedback", json={"verdict": "right"},
                                headers=hd)).json().get("no_reviewer") is None

    async with factory() as s:
        rows = await all_feedback(s)
        from gaia.evaluation.sections import extra_sections

        text, doc = await extra_sections(s, "demo_pg", _auth.settings)
    # the change of mind to "right" kept the note: a verdict alone changes the verdict (D94)
    assert [(r["verdict"], r["note"], r["status"], r["reviewers"]) for r in rows] == [
        ("right", "only half the stores", "none", []), ("wrong", None, "open", [])]
    assert "**nobody reviews demo_pg**: its doubted answers wait for no one" in text
    assert doc["feedback"]["reviewers"] == [] and doc["feedback"]["open"] == 1

    monkeypatch.setenv("GAIA_METADATA_URL", os.environ["GAIA_TEST_DATABASE_URL"])
    await dispose_engine()
    assert await feedback_tool._run(argparse.Namespace(last=10)) == 0
    out = capsys.readouterr().out
    assert "demo_pg" in out and "wrong" in out and "NOBODY reviews this data source" in out
    assert "“only half the stores”" in out


@pytest.mark.asyncio
async def test_a_verdict_given_while_testing_is_set_aside(api, monkeypatch, capsys):  # noqa: F811
    """D96 (OWNER_VERIFICATION §36): akaya's two decisions in A1 were a test ("Test", "Did not
    like the result") — and labels. An administrator sets such a verdict aside: it stays, marked,
    and counts nowhere — no label, no count, no SME item; audited; and can be taken back."""
    import argparse
    import os

    from gaia.db.engine import dispose_engine
    from gaia.evaluation.sections import extra_sections
    from gaia.tools import feedback as feedback_tool

    app, auth, factory = api
    async with await _client(app) as deniz, await _client(app) as ayse:
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        hd = {"X-CSRF-Token": deniz.cookies.get("gaia_csrf")}
        ha = {"X-CSRF-Token": ayse.cookies.get("gaia_csrf")}
        cid = (await deniz.post("/api/conversations", json={"data_source": "demo_pg"},
                                headers=hd)).json()["id"]
        decided, waiting = await _answer(factory, cid, 2), await _answer(factory, cid, 4)
        await deniz.put(f"/api/messages/{decided}/feedback",
                        json={"verdict": "wrong", "note": "Test"}, headers=hd)
        await deniz.put(f"/api/messages/{waiting}/feedback", json={"verdict": "partly"},
                        headers=hd)
        items = (await ayse.get("/api/review/demo_pg/feedback")).json()["items"]
        first = next(i for i in items if i["message_id"] == decided)
        await ayse.post(f"/api/review/demo_pg/feedback/{first['id']}/decision",
                        json={"decision": "right"}, headers=ha)
        async with factory() as s:
            assert len((await answer_points(s))[("deepseek-test", ANSWER_SCORING)]) == 1

        monkeypatch.setenv("GAIA_METADATA_URL", os.environ["GAIA_TEST_DATABASE_URL"])
        await dispose_engine()
        ns = argparse.Namespace
        assert await feedback_tool._run(ns(command="void", id=first["id"][:4], note="x")) == 1
        assert "at least its first 6" in capsys.readouterr().err
        assert await feedback_tool._run(ns(command="void", id=first["id"][:8],
                                           note="clicked while testing A1")) == 0
        second = next(i for i in items if i["message_id"] == waiting)
        assert await feedback_tool._run(ns(command="void", id=second["id"],
                                           note="a test")) == 0
        capsys.readouterr()

        # no label, no SME item, nothing to decide, nothing counted
        async with factory() as s:
            assert (await answer_points(s)).get(("deepseek-test", ANSWER_SCORING)) is None
            text, doc = await extra_sections(s, "demo_pg", auth.settings)
        assert doc["feedback"]["open"] == 0 and doc["feedback"]["decided"] == 0
        assert "set aside: 2 (not counted, no label)" in text
        assert (await ayse.get("/api/review/demo_pg/feedback")).json()["total"] == 0
        assert (await ayse.get("/api/review")).json()[0]["feedback_open"] == 0
        r = await ayse.post(f"/api/review/demo_pg/feedback/{second['id']}/decision",
                            json={"decision": "right"}, headers=ha)
        assert r.status_code == 404

        # still listed, marked; audited; and taken back
        assert await feedback_tool._run(ns(last=10)) == 0
        out = capsys.readouterr().out
        assert out.count("SET ASIDE") == 2 and "clicked while testing A1" in out
        assert await feedback_tool._run(ns(command="unvoid", id=second["id"][:8],
                                           note=None)) == 0
        assert (await ayse.get("/api/review/demo_pg/feedback")).json()["total"] == 1
    async with factory() as s:
        events = (await s.scalars(sa.select(AuthEvent.action).where(
            AuthEvent.action.like("feedback_%void%")).order_by(AuthEvent.seq))).all()
    assert events == ["feedback_voided", "feedback_voided", "feedback_unvoided"]
