"""Phase 4D on ``demo_pg``: the golden runner with a scripted model.

The script makes the model right, wrong, over-cautious or rash on purpose, so every
score the runner computes is checked against a known truth. Also: every reference
of the golden set — the owner's seven included — passes the guard and runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from gaia.db.models.chat import Clarification
from gaia.db.models.semantics import JoinHint
from gaia.db.repositories.query_store import QueryStore
from gaia.dialects.postgres.guard import PostgresGuard
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.llm.base import LLMResponse, Usage
from gaia.profiling.service import ProfileOptions, run_profiling
from gaia.query.ask import AskSettings
from gaia.query.golden import load_golden
from gaia.query.golden_run import AS_STATED, report_markdown, run_golden, summarise
from gaia.query.retrieval_store import load_retrieval_index

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
GOLDEN = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "golden.yaml"


def _plan(decision="answer", sql=None, **kw):
    return json.dumps({"decision": decision, "interpretation": kw.get("interpretation", "x"),
                       "assumptions": [], "sql": sql, "uses": [],
                       "clarification": kw.get("clarification"), "reason": kw.get("reason")})


class GoldenModel:
    """Per question: the first generation answer, and the one after a clarification."""

    def __init__(self, golden):
        self.first: dict[str, str] = {}
        self.after: dict[str, str] = {}
        self.seen: list[tuple[str, str]] = []
        for q in golden.questions:
            if q.expect == "answer":
                self.first[q.question] = _plan(sql=q.sql)
            elif q.sql:  # clarify, with the reference as best guess and as the answer after
                self.first[q.question] = _plan(
                    "clarify", sql=q.sql, interpretation="best guess",
                    clarification={"question": "Which do you mean?", "default": 0,
                                   "options": [q.clarified, "something else"],
                                   "ambiguity": "measure"})
                self.after[q.question] = _plan(sql=q.sql)
            else:
                self.first[q.question] = _plan(
                    "clarify", clarification={"question": "Which do you mean?",
                                              "options": ["a", "b"], "ambiguity": "other"})

    def complete(self, messages, **kw):
        if messages[0].content.startswith("You explain"):
            content = json.dumps({"narrative": "n"})
        else:
            user = messages[1].content
            question = user.rsplit("Question: ", 1)[-1].strip()
            clarified = "Earlier clarifications:" in user
            self.seen.append((question, "after" if clarified else "first"))
            content = (self.after if clarified else self.first)[question]
        return LLMResponse(content=content, model="scripted", finish_reason="stop",
                           usage=Usage(prompt_tokens=10, completion_tokens=5), latency_ms=3)


@pytest.mark.asyncio
async def test_golden_runner_scores_what_it_should(pg_session_factory, tmp_path):
    from test_profiling_pg import _introspect, _settings, _target

    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["legacy.tb_trx02", "crm.customers", "store.stores"]))
        ds = await s.scalar(sa.text("SELECT id FROM catalog.data_sources LIMIT 1"))
        for col, schema, table, to_col in [("f2", "crm", "customers", "customer_code"),
                                           ("f9", "store", "stores", "store_code")]:
            s.add(JoinHint(data_source_id=ds, from_schema="legacy", from_table="tb_trx02",
                           from_columns=[col], to_schema=schema, to_table=table,
                           to_columns=[to_col], source="llm", status="inferred"))
        await s.commit()
        index = await load_retrieval_index(s, "demo_pg", settings)
    golden = load_golden(GOLDEN)
    model = GoldenModel(golden)
    # make the model err on purpose, in four ways
    by_id = {q.id: q for q in golden.questions}
    model.first[by_id["g01"].question] = _plan(sql=by_id["g01"].sql.replace(
        "AND status <> 'cancelled'", ""))                               # wrong: cancelled kept
    model.first[by_id["g04"].question] = _plan(
        "clarify", clarification={"question": "Active how?", "options": ["a", "b"]})
    model.after[by_id["g04"].question] = _plan(sql=by_id["g04"].sql)   # over-clarified, then ok
    model.first[by_id["o03"].question] = _plan(sql=by_id["o03"].sql)   # answered directly
    o06 = by_id["o06"]
    model.first[o06.question] = json.loads(model.first[o06.question]) | {  # a wrong best guess
        "sql": "SELECT p.sku, p.name, (p.list_price - p.unit_cost) AS profit "
               "FROM product.products p ORDER BY profit DESC LIMIT 5"}
    model.first[o06.question] = json.dumps(model.first[o06.question])

    llm = RecordingLLMClient(model, tmp_path / "llm")
    guard = PostgresGuard(index.catalog, row_limit=1000)
    ask_settings = AskSettings(model="scripted", statement_timeout_s=30)
    outcomes = run_golden(golden, index=index, guard=guard, target=_target(), llm=llm,
                          settings=ask_settings, workers=3)
    by = {o.id: o for o in outcomes}
    assert len(outcomes) == 32

    # every reference passed the guard and ran (else run_golden would have raised), and
    # the model's right answers are scored right
    for qid in ("g02", "g03", "g13", "g16", "g17", "g19"):
        assert by[qid].correct is True and by[qid].correct_first is True, by[qid]
    assert by["g01"].correct is False and "'total'" in by["g01"].reason
    assert by["g04"].over_clarified and by["g04"].correct is True
    assert ("How many active customers are there in each customer segment?", "after") in \
        model.seen
    g04_after = [m for m in model.seen if m[1] == "after"]
    assert g04_after  # AS_STATED was sent
    assert by["o03"].answered_directly and by["o03"].correct is True
    assert by["o06"].best_guess == "wrong" and by["o06"].correct is True
    for qid in ("o01", "o02", "o04", "o05", "o07", "c01", "c03"):
        assert by[qid].best_guess == "correct" and by[qid].correct is True, by[qid]
    assert by["c02"].best_guess == "not scored" and by["c02"].correct is None

    summary = summarise(outcomes)
    assert summary["with_reference"] == 30  # 21 answers + 9 clarified with a reference
    assert summary["correct"] == 29 / 30 and summary["executable"] == 1.0
    assert summary["over_clarified"] == 1 and summary["answered_directly"] == 1
    assert summary["best_guess_correct"] == 7 / 8
    text = report_markdown(golden, outcomes, summary, "scripted", "test")
    assert "| Correct result (= the reference) | 97% |" in text
    assert "o06" in text and AS_STATED not in text

    # a re-run replays every model answer
    live = llm.live_calls
    run_golden(golden, index=index, guard=guard, target=_target(), llm=llm,
               settings=ask_settings, only=["o01", "g02"])
    assert llm.live_calls == live

    # clarifications are stored with Gaia's best guess and the user's choice (D61)
    async with factory() as s:
        store = QueryStore(s)
        a = by["o01"].answers[0]
        row = await store.record_clarification(a, data_source_id=index.data_source_id,
                                               snapshot_id=index.snapshot_id, origin="test")
        await store.answer_clarification(row, "1", None)
        other = await store.record_clarification(
            by["o05"].answers[0], data_source_id=index.data_source_id,
            snapshot_id=index.snapshot_id, origin="test")
        await store.answer_clarification(other, "only this year, please", None)
        await s.commit()
        rows = {r.question: r for r in (await s.scalars(sa.select(Clarification))).all()}
    first = rows[by["o01"].question]
    assert first.default_option == 0 and first.best_guess_valid is True
    assert first.best_guess_sql and first.chosen_option == 0 and first.took_default is True
    free = rows[by["o05"].question]
    assert free.chosen_option is None and free.took_default is None and free.status == "answered"
