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
