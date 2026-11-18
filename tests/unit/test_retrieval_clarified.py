"""retrieval-eval with clarified questions and from a golden set (7B, D97; D91)."""

from __future__ import annotations

from pathlib import Path

from gaia.query.ask import search_text
from gaia.tools.retrieve import _golden_questions, _load_questions, searched

ROOT = Path(__file__).resolve().parents[2]


def test_a_clarified_question_is_searched_as_gaia_searches_it():
    items = _load_questions(ROOT / "infra" / "pg-target" / "retrieval_questions.csv")
    o06 = next(x for x in items if x[0] == "Top 5 most profitable products")
    assert o06[1] == [["sales.order_lines", "sales.orders", "product.products"]]
    assert o06[2].startswith("profit realised on sales")
    assert searched(o06[0], o06[2]) == search_text(o06[0], [("", o06[2])])
    assert "not cancelled" in searched(o06[0], o06[2])
    plain = next(x for x in items if x[0].startswith("How many active customers"))
    assert plain[2] is None and searched(plain[0], None) == plain[0]
    assert not any(q.startswith("#") for q, _e, _c in items)


def test_a_golden_set_is_a_retrieval_set_too():
    items = _golden_questions(ROOT / "infra" / "pg-target" / "golden.yaml")
    by = {q: (e, c) for q, e, c in items}
    assert len(items) == 62  # every question with objects; c02, c04, c08, c09 have none
    assert by["Top 5 most profitable products"][0] == [
        ["sales.order_lines", "sales.orders", "product.products"]]
    assert by["Top 5 most profitable products"][1].startswith("profit realised")
    assert by["What was the total order amount in December 2025, excluding cancelled orders?"
              ][1] is None
