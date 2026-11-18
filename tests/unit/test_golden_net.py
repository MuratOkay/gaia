"""The replayed net's comparison (6E, D82), without a database."""

from __future__ import annotations

from gaia.evaluation.net import differences, fingerprint, question_of
from gaia.llm.base import Message


def test_what_a_call_is_about():
    gen = [Message("system", "You write one read-only SQL query"),
           Message("user", "Dialect: PostgreSQL\n\nQuestion: Top shops?")]
    assert question_of(gen) == ("Top shops?", "generation")
    assert question_of([*gen, Message("assistant", "{}"), Message("user", "rejected")])[1] == \
        "repair"
    narr = [Message("system", "You explain the result"),
            Message("user", 'Answer this (json):\n{"question": "Top shops?"}')]
    assert question_of(narr) == ("Top shops?", "narration")


def test_estimates_do_not_move_a_prompt_but_words_do():
    a = [Message("user", "TABLE sales.orders — ~250,000 rows; ~2,974 distinct values")]
    b = [Message("user", "TABLE sales.orders — ~249,871 rows; ~2,981 distinct values")]
    c = [Message("user", "TABLE sales.orders — about 250,000 rows")]
    assert fingerprint(a) == fingerprint(b) != fingerprint(c)


def test_a_narrations_rows_count_as_a_set():
    """D83 on the owner's host: g08's and g09's groups (no ORDER BY) came in another order."""
    import json

    def narration(rows):
        doc = {"question": "Per department?", "result": {"columns": ["d", "n"], "rows": rows}}
        return [Message("system", "You explain the result of a database query"),
                Message("user", "Answer this (json):\n" + json.dumps(doc, indent=1))]

    assert fingerprint(narration([["HR", 3], ["IT", 5]])) == \
        fingerprint(narration([["IT", 5], ["HR", 3]]))
    assert fingerprint(narration([["HR", 3], ["IT", 5]])) != \
        fingerprint(narration([["HR", 3], ["IT", 6]]))
    # a generation prompt's order still counts
    gen = [Message("system", "You write one read-only SQL query"), Message("user", "a\nb")]
    assert fingerprint(gen) != fingerprint([gen[0], Message("user", "b\na")])


def test_differences_in_words():
    def q(prompts, **outcome):
        return {"prompts": [{"purpose": p, "prompt": h} for p, h in prompts],
                "outcome": {"repairs": 0, "correct": True, **outcome}}

    base = {"g01": q([("generation", "a"), ("narration", "b")]),
            "g02": q([("generation", "c"), ("narration", "d")])}
    assert differences(base, base) == []
    moved = differences(base, {"g01": q([("generation", "a"), ("narration", "b")], repairs=1),
                               "g02": q([("generation", "X"), ("narration", "d")])})
    assert moved == ["g01: the outcome changed — repairs 0 → 1",
                     "g02: the prompt changed — generation (call 1)"]
    everything = differences(base, {k: q([("generation", "Z"), ("narration", "Y")])
                                    for k in base})
    assert everything[-1].startswith("every prompt moved and no outcome did")


def test_a_deliberate_regression_removes_one_rule_only():
    """6E part 2 (D87): ``make golden ARGS="--regress joins-rule"`` sends the SQL prompt without
    its JOINS rule — nothing else — so three such runs measure whether the fresh net catches it."""
    import pytest

    from gaia.evaluation.net import PromptRegression
    from gaia.query.planner import GENERATION_MAJOR
    from gaia.query.prompts import query_prompt

    seen = []

    class Model:
        model = "m"
        live_calls = 3

        def complete(self, messages, **kw):
            seen.append(messages)
            return "ok"

    system = query_prompt("generate_sql", GENERATION_MAJOR).text
    wrapped = PromptRegression(Model(), "joins-rule")
    wrapped.complete([Message("system", system), Message("user", "Question: x")])
    sent = seen[-1][0].content
    assert "Join only as listed under JOINS" in system and "Join only as listed" not in sent
    assert len(sent.splitlines()) == len(system.splitlines()) - 1
    assert seen[-1][1].content == "Question: x" and wrapped.applied == 1
    narration = [Message("system", "You explain the result of a database query"),
                 Message("user", "Join only as listed under JOINS.")]
    wrapped.complete(narration)
    assert seen[-1] == narration and wrapped.applied == 1 and wrapped.live_calls == 3
    with pytest.raises(ValueError, match="no longer applies"):
        wrapped.complete([Message("system", "You write one read-only SQL query. No rules.")])
    with pytest.raises(ValueError, match="unknown regression"):
        PromptRegression(Model(), "nothing")
