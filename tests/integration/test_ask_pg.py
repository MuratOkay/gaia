"""Phase 4C on ``demo_pg`` with a scripted model: every path of ``ask`` against the
real guard, the real database and the real store, and the golden set's reference
SQL through the whole pipeline.

The scripted model answers what each test tells it to; nothing here measures a
model (that is 4D, on the owner's host)."""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest
import sqlalchemy as sa

from gaia.db.models.audit import LLMCall
from gaia.db.models.chat import QueryRun
from gaia.db.repositories.query_store import QueryStore
from gaia.dialects.postgres.guard import PostgresGuard
from gaia.llm.base import LLMResponse, Usage
from gaia.profiling.service import ProfileOptions, run_profiling
from gaia.query.ask import AskSettings, ask
from gaia.query.retrieval_store import load_retrieval_index

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
TODAY = date(2026, 1, 15)


def plan(sql=None, decision="answer", **kw):
    doc = {"decision": decision, "interpretation": kw.pop("interpretation", "what it computes"),
           "assumptions": kw.pop("assumptions", []), "sql": sql, "uses": kw.pop("uses", []),
           "clarification": kw.pop("clarification", None), "reason": kw.pop("reason", None)}
    return json.dumps(doc)


class ScriptedModel:
    """Answers a question's generation and repair calls from a script, in order; the
    narrative call with a fixed text. Keeps every request."""

    def __init__(self, scripts: dict[str, list[str]]):
        self.scripts = {q: list(v) for q, v in scripts.items()}
        self.requests: list[tuple[str, list]] = []

    def complete(self, messages, **kw):
        system = messages[0].content
        if system.startswith("You explain the result"):
            self.requests.append(("answer", messages))
            content = json.dumps({"narrative": "Here is what the data says."})
        else:
            question = messages[1].content.rsplit("Question: ", 1)[-1].strip()
            purpose = "sql_repair" if len(messages) > 2 else "sql_generation"
            self.requests.append((purpose, messages))
            content = self.scripts[question].pop(0)
        return LLMResponse(content=content, model="scripted", finish_reason="stop",
                           usage=Usage(prompt_tokens=100, completion_tokens=20), latency_ms=7)


def _settings_for(settings, **kw):
    base = {"model": "scripted", "row_limit": 1000, "max_repair_attempts": 2,
            "statement_timeout_s": 5}
    base.update(kw)
    return AskSettings(**base)


async def _setup(factory, tables):
    from test_profiling_pg import _introspect, _settings

    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg", tables=tables))
    return settings


@pytest.mark.asyncio
async def test_every_path_of_ask(pg_session_factory):
    from test_profiling_pg import _target

    factory = pg_session_factory
    settings = await _setup(factory, ["crm.customers", "store.stores", "sales.orders"])
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    guard = PostgresGuard(index.catalog, row_limit=1000)
    good = ("SELECT s.city, sum(o.total_amount) AS total_amount FROM sales.orders o "
            "JOIN store.stores s ON s.store_id = o.store_id "
            "WHERE o.order_date >= DATE '2025-12-01' AND o.order_date < DATE '2026-01-01' "
            "GROUP BY s.city ORDER BY total_amount DESC LIMIT 3")
    q_ok = "Which three store cities sold the most in December 2025?"
    q_dberr = "What is the sum of the store cities as numbers?"
    q_slow = "Count the orders of a very long series"
    q_never = "A question about orders the model keeps getting wrong"
    q_ask = "Who are our best customers?"
    q_no = "What is our market share of all orders in Turkey?"
    q_mail = "How many customers share each e-mail address?"
    model = ScriptedModel({
        q_ok: [plan(good, uses=["sales.orders.total_amount", "store.stores.city"])],
        q_dberr: [plan("SELECT sum(CAST(s.city AS integer)) AS n FROM store.stores s"),
                  plan("SELECT count(*) AS n FROM store.stores s")],
        q_slow: [plan("SELECT count(*) AS n FROM generate_series(1, 400000000) AS g(x)"),
                 plan("SELECT count(*) AS n FROM generate_series(1, 10) AS g(x)")],
        q_never: [plan("SELECT pg_sleep(1)"), plan("DELETE FROM sales.orders"),
                  plan("SELECT * FROM pg_catalog.pg_authid")],
        q_ask: [plan(decision="clarify", clarification={
            "question": "Best by what?", "options": ["order amount", "number of orders"]})],
        q_no: [plan(decision="cannot_answer", reason="the market's size is not stored")],
        q_mail: [plan("SELECT c.email, count(*) AS customers FROM crm.customers c "
                      "WHERE c.email IS NOT NULL GROUP BY c.email ORDER BY 2 DESC LIMIT 5")],
    })
    target = _target()

    def run(q, **kw):
        return ask(q, index=index, guard=guard, target=target, llm=model,
                   settings=_settings_for(settings, **kw), today=TODAY)

    ok = run(q_ok)
    assert ok.status == "answered" and len(ok.attempts) == 1, ok.reason
    assert ok.columns == ["city", "total_amount"] and len(ok.rows) == 3
    with psycopg.connect(URL) as conn:
        assert [tuple(r) for r in ok.rows] == [tuple(r) for r in conn.execute(good).fetchall()]
    assert isinstance(ok.rows[0][1], Decimal)
    assert ok.narrative == "Here is what the data says."
    assert ok.confidence is not None and ok.sql.rstrip().endswith("LIMIT 3")
    assert [p for p, _m in model.requests] == ["sql_generation", "answer"]

    fixed = run(q_dberr)
    assert fixed.status == "answered" and len(fixed.attempts) == 2
    first = fixed.attempts[0]
    assert first.guard.ok and first.execution.status == "error"
    assert "invalid input syntax" in first.problem
    repair = model.requests[-2][1]  # generation, repair, answer
    assert [m.role for m in repair] == ["system", "user", "assistant", "user"]
    assert "failed on the database" in repair[3].content

    slow = run(q_slow, statement_timeout_s=1)
    assert slow.status == "answered" and slow.attempts[0].execution.status == "timeout"
    assert "ran longer than the time limit (1 s)" in slow.attempts[0].problem

    never = run(q_never)
    assert never.status == "failed" and len(never.attempts) == 3
    assert "no valid query after 2 repair(s)" in never.reason
    assert all(a.execution is None for a in never.attempts)  # nothing refused ever ran

    clarify = run(q_ask)
    assert clarify.status == "clarify" and clarify.clarification.options == [
        "order amount", "number of orders"] and not clarify.attempts
    nothing = run(q_no)
    assert nothing.status == "cannot_answer" and "market" in nothing.reason

    mail = run(q_mail)
    assert mail.status == "answered" and "@" in str(mail.rows[0][0])  # the user sees it
    sent = model.requests[-1][1][1].content
    assert "@" not in sent and "[hidden]" in sent  # a remote model does not (D24)

    # recording: one run per attempt, chained; every model call in the audit log
    async with factory() as s:
        store = QueryStore(s)
        runs = await store.record_answer(fixed, data_source_id=index.data_source_id,
                                         snapshot_id=index.snapshot_id, origin="test")
        await store.record_answer(clarify, data_source_id=index.data_source_id,
                                  snapshot_id=index.snapshot_id, origin="test")
        await s.commit()
        rows = (await s.scalars(sa.select(QueryRun).order_by(QueryRun.attempt))).all()
        calls = (await s.scalars(sa.select(LLMCall))).all()
    assert [(r.attempt, r.status) for r in rows] == [(1, "error"), (2, "ok")]
    assert rows[1].parent_id == rows[0].id == runs[0].id and rows[1].model == "scripted"
    assert rows[0].question == q_dberr and rows[1].prompt_tokens == 100
    assert [c.purpose for c in calls] == ["sql_generation", "sql_repair", "answer",
                                          "sql_generation"]
