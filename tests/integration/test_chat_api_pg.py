"""Phase 4E on ``demo_pg``: the chat API end to end with a scripted model.

Conversations, a streamed answer, a follow-up read with the conversation so far, a
clarification turn answered by an option's number (D61: Gaia's option answers from
its stored best guess), personal data masked at rest but not in the live answer,
the JSON mode, and the errors a client can make."""

from __future__ import annotations

import json
import os

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from gaia.api.chat import ChatService, router
from gaia.db.models.chat import Clarification, Message
from gaia.llm.base import LLMResponse, Usage
from gaia.profiling.service import ProfileOptions, run_profiling

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")

TOP3 = ("SELECT s.city, sum(o.total_amount) AS total_amount FROM sales.orders o "
        "JOIN store.stores s ON s.store_id = o.store_id WHERE o.order_date >= DATE '{0}' "
        "AND o.order_date < DATE '{1}' GROUP BY s.city ORDER BY total_amount DESC LIMIT 3")
VALUABLE = ("SELECT c.customer_code, sum(o.total_amount) AS total_amount FROM sales.orders o "
            "JOIN crm.customers c ON c.customer_id = o.customer_id "
            "WHERE o.status <> 'cancelled' GROUP BY c.customer_code "
            "ORDER BY total_amount DESC LIMIT 20")


def plan(sql=None, decision="answer", interpretation="what it computes", **kw):
    return json.dumps({"decision": decision, "interpretation": interpretation,
                       "assumptions": [], "sql": sql, "uses": [], "clarification":
                       kw.get("clarification"), "reason": kw.get("reason")})


class Model:
    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.requests = []

    def complete(self, messages, **kw):
        if messages[0].content.startswith("You explain"):
            self.requests.append(("answer", messages))
            return LLMResponse(content=json.dumps({"narrative": "Narrative."}), model="s",
                               finish_reason="stop", usage=Usage(prompt_tokens=3))
        question = messages[1].content.rsplit("Question: ", 1)[-1].strip()
        self.requests.append(("generation", messages))
        return LLMResponse(content=self.scripts[question].pop(0), model="s",
                           finish_reason="stop", usage=Usage(prompt_tokens=10))


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = [x for x in block.splitlines() if x and not x.startswith(":")]
        if not lines:
            continue
        name = next(x[7:] for x in lines if x.startswith("event: "))
        data = json.loads(next(x[6:] for x in lines if x.startswith("data: ")))
        out.append((name, data))
    return out


@pytest.mark.asyncio
async def test_chat_api(pg_session_factory):
    from test_profiling_pg import _introspect, _settings

    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["crm.customers", "store.stores", "sales.orders"]))
    q1 = "Which three store cities sold the most in December 2025?"
    q2 = "And in November 2025?"
    q3 = "Who are our most valuable customers?"
    q4 = "Which customers have which e-mail address?"
    q5 = "How many stores are there?"
    model = Model({
        q1: [plan(TOP3.format("2025-12-01", "2026-01-01"),
                  interpretation="Top three store cities by order amount, December 2025")],
        q2: [plan(TOP3.format("2025-11-01", "2025-12-01"),
                  interpretation="Top three store cities by order amount, November 2025")],
        q3: [plan(VALUABLE, "clarify", interpretation="top 20 by order amount, all time",
                  clarification={"question": "Valuable by what?", "default": 0,
                                 "ambiguity": "ranking",
                                 "options": ["total order amount, all time",
                                             "number of orders"]}),
             plan(VALUABLE, "clarify", interpretation="top 20 by order amount, all time",
                  clarification={"question": "Valuable by what?", "default": 0,
                                 "ambiguity": "ranking",
                                 "options": ["total order amount, all time",
                                             "number of orders"]})],
        q4: [plan("SELECT c.customer_code, c.email FROM crm.customers c "
                  "WHERE c.email IS NOT NULL ORDER BY c.customer_code LIMIT 5")],
        q5: [plan("SELECT count(*) AS stores FROM store.stores")] * 2,
    })
    service = ChatService(settings, session_factory=factory, llm_factory=lambda: model,
                          use_vectors=False)
    loads = []
    original = service.index

    async def counting(session, name, ds_id):
        before = dict(service._indexes)
        index = await original(session, name, ds_id)
        loads.append(before.get(name, (None, None, None))[2] is not index)
        return index

    service.index = counting
    app = FastAPI()
    app.include_router(router)
    app.state.chat = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gaia") as client:
        r = await client.get("/api/data-sources")
        demo = next(d for d in r.json() if d["name"] == "demo_pg")
        assert demo | {"name": "demo_pg", "dialect": "postgres", "introspected": True,
                       "usable": True, "default": True, "origin": "config"} == demo
        r = await client.post("/api/conversations", json={"data_source": "demo_pg"})
        assert r.status_code == 201
        cid = r.json()["id"]

        async def say(content, **kw):
            r = await client.post(f"/api/conversations/{cid}/messages",
                                  json={"content": content, "as_of": "2026-01-15", **kw})
            assert r.status_code == 200 and r.headers["content-type"].startswith(
                "text/event-stream")
            return events(r.text)

        # a streamed answer: the stages in order, the table before the narrative
        ev = await say(q1)
        names = [n for n, _d in ev]
        assert names[0] == "message" and names[-2:] == ["answer", "done"]
        stages = [d["stage"] for n, d in ev if n == "status"]
        assert stages == ["retrieval", "retrieved", "generation", "checking", "running",
                          "narrating"]
        assert names.index("result") < names.index("answer")
        early = dict(ev)["result"]
        assert early["columns"] == ["city", "total_amount"] and len(early["rows"]) == 3
        answer = dict(ev)["answer"]
        assert answer["narrative"] == "Narrative." and len(answer["rows"]) == 3
        assert "sql" not in answer  # P4-2: not for users
        assert answer["interpretation"].endswith("December 2025")

        # a follow-up is read with the conversation so far
        ev = await say(q2)
        assert dict(ev)["answer"]["interpretation"].endswith("November 2025")
        sent = model.requests[-2][1][1].content
        assert "Conversation so far:\n- User: " + q1 in sent
        assert "Computed: Top three store cities by order amount, December 2025" in sent

        # a clarification, answered with the number of Gaia's own option (D61)
        ev = await say(q3)
        clar = dict(ev)["clarification"]
        assert clar["clarification"]["options"][0] == "total order amount, all time"
        assert "default" in clar["clarification"] and clar["clarification"]["id"]
        calls_before = sum(1 for p, _m in model.requests if p == "generation")
        ev = await say("1")
        assert dict(ev)["message"]["kind"] == "clarification_answer"
        reply = dict(ev)["answer"]
        assert len(reply["rows"]) == 20 and reply["question"] == q3
        assert sum(1 for p, _m in model.requests if p == "generation") == calls_before

        # a new question while a clarification is open
        ev = await say(q3)
        assert names and dict(ev)["clarification"]
        ev = await say(q5, reply_to_clarification=False)
        assert dict(ev)["message"]["kind"] == "question"
        assert dict(ev)["answer"]["rows"] == [[40]]

        # personal data: in the live answer, masked in the stored turn
        ev = await say(q4)
        live = dict(ev)["answer"]
        assert any("@" in str(r[1]) for r in live["rows"])
        r = await client.get(f"/api/conversations/{cid}")
        conv = r.json()
        stored = conv["messages"][-1]["payload"]
        assert stored["masked_columns"] == [1]
        assert all(row[1] == "[hidden]" for row in stored["rows"])
        assert [m["kind"] for m in conv["messages"]] == [
            "question", "answer", "question", "answer", "question", "clarification",
            "clarification_answer", "answer", "question", "clarification", "question",
            "answer", "question", "answer"]
        assert conv["title"] == q1

        # JSON instead of a stream
        r = await client.post(f"/api/conversations/{cid}/messages?stream=false",
                              json={"content": q5})
        assert r.status_code == 200 and r.json()["reply"]["rows"] == [[40]]
        assert [e["data"]["stage"] for e in r.json()["events"]][:2] == ["retrieval", "retrieved"]

        # an internal error ends the turn as "failed", and the conversation goes on
        ev = await say("How many stores are there in total?")  # the model has no script
        failed = dict(ev)["failed"]
        assert failed["reason"].startswith("internal error: KeyError")

        # client errors
        r = await client.post("/api/conversations/00000000-0000-0000-0000-000000000000"
                              "/messages", json={"content": "x"})
        assert r.status_code == 404
        r = await client.post(f"/api/conversations/{cid}/messages", json={"content": ""})
        assert r.status_code == 422
        r = await client.get("/api/conversations", params={"data_source": "nope"})
        assert r.status_code == 404
        r = await client.get("/api/conversations", params={"data_source": "demo_pg"})
        assert [c["id"] for c in r.json()] == [cid]

    assert loads[0] and not any(loads[1:])  # the index was loaded once, then reused
    async with factory() as s:
        rows = (await s.scalars(sa.select(Clarification).order_by(
            Clarification.created_at))).all()
        assert [(c.origin, c.status, c.took_default) for c in rows] == [
            ("api", "answered", True), ("api", "open", None)]
        runs = await s.scalar(sa.text("SELECT count(*) FROM chat.query_run WHERE origin='api'"))
        assert runs >= 5
        assert await s.scalar(sa.select(sa.func.count()).select_from(Message)) == 18


@pytest.mark.asyncio
async def test_a_slow_model_gets_keepalives(pg_session_factory, monkeypatch):
    import time

    from test_profiling_pg import _introspect, _settings

    import gaia.api.chat as chat_module

    monkeypatch.setattr(chat_module, "KEEPALIVE_S", 0.05)
    await _introspect(pg_session_factory)

    class Slow(Model):
        def complete(self, messages, **kw):
            time.sleep(0.3)
            return super().complete(messages, **kw)

    q = "How many stores are there?"
    service = ChatService(_settings(), session_factory=pg_session_factory,
                          llm_factory=lambda: Slow({q: [plan(
                              "SELECT count(*) AS stores FROM store.stores")]}),
                          use_vectors=False)
    app = FastAPI()
    app.include_router(router)
    app.state.chat = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gaia") as client:
        cid = (await client.post("/api/conversations", json={"data_source": "demo_pg"})
               ).json()["id"]
        r = await client.post(f"/api/conversations/{cid}/messages", json={"content": q})
    assert ": keep-alive" in r.text
    assert dict(events(r.text))["answer"]["rows"] == [[40]]
