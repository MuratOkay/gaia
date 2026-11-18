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
