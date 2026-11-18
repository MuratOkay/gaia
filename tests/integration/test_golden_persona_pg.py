"""Golden runs as a person (Phase 7B, D97) on ``demo_pg``: need-to-know measured.

Berk reads Sales, Store, Product and Crm and is not a PII processor (test_need_to_know_pg's
world). A question whose objects he may not read — or that needs personal data — is
*withheld*: right only when no number comes back; a number is one computed from other data.
Every answer is searched for the names of what he may not read. The references always run
with the operator's rights."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _access import categorize, person, policy_of
from test_golden_pg import GoldenModel, _plan
from test_review_pg import _world

from gaia.access.policy import OPERATOR, build_access_guard
from gaia.query.ask import AskSettings
from gaia.query.golden import load_golden
from gaia.query.golden_run import report_markdown, run_golden, summarise, withheld_from

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
GOLDEN = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "golden.yaml"
ONLY = ["g01", "g02", "g07", "h10", "h12", "h16", "h29", "c08"]


@pytest.mark.asyncio
async def test_a_golden_run_as_a_person(pg_session_factory, fake_llm, fake_embedder):
    from test_profiling_pg import _target

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    cats = await categorize(factory)
    berk = person("berk", [cats[c] for c in ("Sales", "Store", "Product", "Crm")])
    ece = person("ece", [cats[c] for c in ("Sales", "Store", "Product", "Crm")], pii=True)
    golden = load_golden(GOLDEN)
    by = {q.id: q for q in golden.questions}

    index, _facts, policy, restricted = await policy_of(factory, settings, berk)
    assert withheld_from(by["g02"], policy) == []
    assert withheld_from(by["g07"], policy) == ["finance.invoices"]
    assert withheld_from(by["h29"], policy) == ["personal data"]
    assert withheld_from(by["h29"], OPERATOR) == []

    model = GoldenModel(golden)  # the reference as the answer: refused where withheld
    # h10 withheld (finance), but the model finds "something" in what Berk may read
    model.first[by["h10"].question] = _plan(sql=(
        "SELECT count(*) AS invoices, sum(total_amount) AS total_amount FROM sales.orders "
        "WHERE status = 'cancelled'"))
    # g07 withheld (finance): the model asks back — and its words name the forbidden tables
    model.first[by["g07"].question] = _plan("clarify", clarification={
        "question": "Should I read overdue amounts from finance.invoices or from "
                    "finance.gl_entries?", "options": ["the invoices", "the ledger"],
        "ambiguity": "entity"})
    model.after[by["g07"].question] = _plan("cannot_answer", reason="not in the data shown")

    def run(who, pol, idx):
        return run_golden(
            golden, index=idx,
            guard=build_access_guard(settings, "demo_pg", idx, index.catalog, pol),
            target=_target(), llm=model, settings=AskSettings(model="scripted",
                                                              statement_timeout_s=30),
            only=ONLY, workers=2, person=who,
            reference_guard=build_access_guard(settings, "demo_pg", index, index.catalog,
                                               OPERATOR))

    got = {o.id: o for o in run(policy, policy, restricted)}
    # allowed questions are scored as usual
    assert got["g01"].correct is True and got["g02"].correct is True
    assert not got["g02"].withheld and got["g02"].leaked == []
    # withheld: the model reached for hr (the guard refused), or nothing he may read matched
    # (asked back, twice, without a model call) — no number either way: right
    assert got["h12"].withheld == ["hr.employees", "hr.departments"]
    assert got["h12"].final_status == "refused" and got["h12"].correct is True
    assert got["h16"].final_status == "clarify" and got["h16"].calls == 0
    assert got["h16"].correct is True and got["h16"].executable is None
    assert got["h29"].withheld == ["personal data"] and got["h29"].final_status == "refused"
    assert got["h29"].correct is True
    # withheld, answered from other data: wrong, and says why
    assert got["h10"].final_status == "answered" and got["h10"].correct is False
    assert "answered although it needs finance.invoices" in got["h10"].reason
    # withheld, no number — but a forbidden name in what Gaia said: wrong
    assert got["g07"].withheld == ["finance.invoices"]
    assert got["g07"].final_status == "cannot_answer"
    assert got["g07"].leaked == ["finance.gl_entries", "finance.invoices"]
    assert got["g07"].correct is False and "named what the person may not read" in \
        got["g07"].reason
    # a question with no data at all is asked back, for anyone
    assert got["c08"].withheld == [] and got["c08"].first_status == "clarify"

    summary = summarise(list(got.values()))
    assert summary["withheld"] == 5 and summary["withheld_kept"] == 4
    assert summary["leaks"] == ["finance.gl_entries", "finance.invoices"]
    assert summary["leaked_questions"] == ["g07"]
    assert summary["with_reference"] == 2 and summary["correct"] == 1.0  # allowed ones only
    text = report_markdown(golden, list(got.values()), summary, "scripted", "test")
    assert "## As this person (need-to-know, 7B)" in text
    assert "h10: withheld for finance.invoices → answered — **wrong**" in text

    # a PII processor may filter on personal data: h29 is hers, answered and right
    _i, _f, ece_policy, ece_index = await policy_of(factory, settings, ece)
    both = {o.id: o for o in run(ece_policy, ece_policy, ece_index)}
    assert both["h29"].withheld == [] and both["h29"].correct is True

    # the operator: nothing withheld, nothing leaked, the ordinary run
    op = {o.id: o for o in run(None, OPERATOR, index)}
    assert all(not o.withheld and not o.leaked for o in op.values())
    assert op["h10"].correct is False  # the scripted wrong answer, scored as such
    assert json.loads(model.first[by["g02"].question])["sql"] == by["g02"].sql
