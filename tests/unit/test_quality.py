"""The quality report (Phase 6A, D80): intervals, flaky questions, replays left out, and a
comparison question by question (P6-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaia.evaluation.quality import (
    ItemView,
    RunView,
    compare,
    quality_report,
    select_runs,
    wilson,
)

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def run(rid: str, rights: dict[str, bool], *, build="B1", mode="fresh", n=0) -> RunView:
    items = {k: ItemView(k, "answer", ok, True, "answered", "answered", calls=2, tokens=100,
                         seconds=1.0 + i / 10, reason=None if ok else f"{k} is wrong")
             for i, (k, ok) in enumerate(rights.items())}
    return RunView(rid, T0 + timedelta(hours=n), build, "m", mode, items=items, set_key="S")


def thirty(wrong: set[str]) -> dict[str, bool]:
    return {f"q{i:02}": f"q{i:02}" not in wrong for i in range(30)}


def test_wilson():
    lo, hi = wilson(29, 30)
    assert lo == pytest.approx(0.8333, abs=1e-3) and hi == pytest.approx(0.9941, abs=1e-3)
    lo, hi = wilson(27, 30)  # the 90 % of the D68–D71 runs: not distinguishable from 97 %
    assert lo < 0.8 and hi > 0.96
    assert wilson(0, 0) is None and wilson(0, 5)[0] == 0.0


def test_the_report_pools_runs_and_names_flaky_questions():
    runs = [run("aaaa1111", thirty({"q07"}), n=0), run("bbbb2222", thirty({"q07", "q12"}), n=1),
            run("cccc3333", thirty({"q07", "q13"}), n=2)]
    replay = run("dddd4444", thirty({"q07"}), mode="replay", n=3)
    text, doc = quality_report(runs, data_source="demo_pg", question_set_key="S",
                               excluded=[replay])
    assert doc["correct"]["k"] == 85 and doc["correct"]["n"] == 90
    assert "85 of 90 = 94% (95% interval" in text
    assert doc["questions"]["never"] == ["q07"] and "q07: q07 is wrong" in text
    assert doc["questions"]["flaky"] == [["q12", 2, 3], ["q13", 2, 3]]
    assert doc["excluded"] == ["dddd4444"] and "replay — left out" in text
    assert "1 replay(s) left out" in text


def test_a_comparison_passes_fails_or_cannot_tell():
    base = [run(f"b{i}", thirty({"q07"}), n=i) for i in range(3)]
    same = [run(f"s{i}", thirty({"q07"}), n=5 + i) for i in range(3)]
    c = compare(base, same)
    assert c.verdict == "passes" and c.questions == 30 and c.difference == 0
    # one question wrong once in three runs: too little to rule out a 3-point loss
    flip = [run(f"f{i}", thirty({"q07"} | ({"q12"} if i == 0 else set())), n=5 + i)
            for i in range(3)]
    c = compare(base, flip)
    assert c.verdict == "cannot tell" and c.low < -0.03 < c.high
    worse = [run(f"w{i}", thirty({"q07", "q01", "q02", "q03", "q04", "q05"}), n=9 + i)
             for i in range(3)]
    c = compare(base, worse)
    assert c.verdict == "fails" and c.difference == pytest.approx(-5 / 30)
    assert [m[0] for m in c.moved] == ["q01", "q02", "q03", "q04", "q05"]
    one = [run("o1", thirty({"q07", "q01"}), n=20)]
    assert compare(base[:1], one).verdict == "cannot tell"
    # reproducible: the bootstrap is seeded
    assert compare(base, worse).low == compare(base, worse).low


def test_which_runs_a_report_reads():
    runs = [run("a1", thirty(set()), build="B1", n=0), run("a2", thirty(set()), build="B1", n=1),
            run("r1", thirty(set()), build="B1", mode="replay", n=2),
            run("c1", thirty({"q01"}), build="B2", n=3), run("c2", thirty(set()), build="B2", n=4),
            run("c3", thirty(set()), build="B2", n=5)]
    other = RunView("x1", T0, "B2", "m", "fresh", items={}, set_key="OTHER")
    sel = select_runs([*runs, other], set_key="S", last=3)
    assert [r.id for r in sel.shown] == ["c1", "c2", "c3"] and [r.id for r in sel.excluded] == ["r1"]
    sel = select_runs(runs, compare_builds=True)
    assert [r.id for r in sel.baseline] == ["a1", "a2"]
    assert [r.id for r in sel.candidate] == ["c1", "c2", "c3"]
    sel = select_runs(runs, baseline_ids=["a1", "r1"], candidate_ids=["c"])
    assert [r.id for r in sel.baseline] == ["a1"]  # a replay is never a measurement
    text, doc = quality_report(sel.shown, data_source="d", question_set_key="S",
                               baseline=sel.baseline, candidate=sel.candidate)
    assert "## Candidate against baseline (P6-2)" in text and doc["comparison"]["questions"] == 30


def test_a_comparison_says_when_the_meanings_differ():
    """D81: on the owner's host g07 went from wrong to right between D77 and D80 — because of
    his SME decision, not the build. The report now says when the runs' meanings differ."""
    from gaia.evaluation.quality import meanings_note

    def with_meanings(r: RunView, key: str | None, decided: int) -> RunView:
        r.meanings_key, r.meanings = key, ({"sme_decisions": decided} if key else {})
        return r

    base = [with_meanings(run(f"b{i}", thirty({"q07"}), n=i), "aaa111", 0) for i in range(2)]
    cand = [with_meanings(run(f"c{i}", thirty(set()), n=5 + i), "bbb222", 1) for i in range(2)]
    note = meanings_note(base, cand)
    assert note and "different meanings (SME decisions 0 → 1" in note
    text, doc = quality_report([*base, *cand], data_source="d", question_set_key="S",
                               baseline=base, candidate=cand)
    assert "not the build's alone" in text and doc["comparison"]["meanings"] == note
    assert doc["comparison"]["per_run"]["baseline"]["calls"] == 60  # 30 questions × 2 calls
    assert "* per run: repairs 0.0 → 0.0, model calls 60 → 60" in text
    assert "`aaa111` (0 SME)" in text
    same = [with_meanings(run(f"s{i}", thirty(set()), n=9 + i), "aaa111", 0) for i in range(2)]
    assert meanings_note(base, same) is None
    imported = [with_meanings(run("i1", thirty(set()), mode="imported"), None, 0)]
    assert "unknown" in meanings_note(imported, same)


def test_a_question_wrong_in_every_run_shows_its_latest_sql():
    runs = [run(f"r{i}", thirty({"q07"}), n=i) for i in range(2)]
    runs[-1].items["q07"].sql = "SELECT sum(x) AS profit\nFROM t"
    text, doc = quality_report(runs, data_source="d", question_set_key="S")
    assert doc["questions"]["never_sql"] == {"q07": "SELECT sum(x) AS profit\nFROM t"}
    assert "    ```sql\n    SELECT sum(x) AS profit\n    FROM t\n    ```" in text


def test_why_answers_were_wrong_and_the_confidence_shown():
    """D84: each item keeps what its final answer was made of. o06 on the owner's host was
    wrong in all seven runs — the report now says whether the context lacked an object the
    reference needs (retrieval) or held them all (the model's reading)."""
    runs = [run(f"r{i}", thirty({"q06", "q07"}), n=i) for i in range(2)]
    for r in runs:
        r.items["q06"].detail = {"objects": ["sales.order_lines", "product.products"],
                                 "missing": ["sales.orders"],
                                 "interpretation": "Profit per product over all orders",
                                 "assumptions": ["all statuses"],
                                 "clarification": ["How is profit measured?",
                                                   "line subtotal minus cost, not cancelled"]}
        r.items["q07"].detail = {"objects": ["finance.invoices"], "missing": [],
                                 "confidence": {"level": "high", "score": 0.86}}
        for k in ("q00", "q01", "q02"):
            r.items[k].detail = {"objects": ["x"], "missing": [],
                                 "confidence": {"level": "high", "score": 0.9}}
        r.items["q03"].detail = {"objects": ["x"], "missing": [],
                                 "confidence": {"level": "low", "score": 0.4}}
    text, doc = quality_report(runs, data_source="d", question_set_key="S")
    assert "the context lacked sales.orders (retrieval)" in text
    assert "answered after: “line subtotal minus cost, not cancelled”" in text
    assert "Gaia understood: Profit per product over all orders" in text
    assert "held every object the reference needs (the model's reading)" in text
    causes = doc["wrong_causes"]
    assert (causes["wrong"], causes["retrieval"], causes["reading"], causes["unknown"]) == (
        4, 2, 2, 0)
    assert causes["retrieval_questions"] == ["q06"] and causes["reading_questions"] == ["q07"]
    assert doc["confidence_levels"] == {"high": [6, 8], "low": [2, 2]}
    assert "shown as high: right 6 of 8 = 75%" in text


def test_unknown_meanings_say_why():
    from gaia.evaluation.quality import meanings_note

    known = [run("k1", thirty(set()))]
    known[0].meanings_key = "aaa"
    old = [run("o1", thirty(set()))]  # a fresh run recorded before D81 kept the meanings
    assert "recorded before build D81" in meanings_note(old, known)
    imp = [run("i1", thirty(set()), mode="imported")]
    assert "(imported runs)" in meanings_note(imp, known)


def test_a_deliberate_regression_measures_the_net_not_a_build():
    """D89 (OWNER_VERIFICATION §30): the host's three ``--regress joins-rule`` runs became the
    newest "build" — ``--last 3`` would have reported them as the current quality, and the next
    build would have been compared with them. They are left out of both, except as the
    comparison they were made for: the regression against the build it was made of."""
    reg = "B2 + deliberate regression: joins-rule"
    runs = [*(run(f"a{i}", thirty(set()), build="B1", n=i) for i in range(3)),
            *(run(f"b{i}", thirty({"q01"} if i == 0 else set()), build="B2", n=3 + i)
              for i in range(3)),
            *(run(f"r{i}", thirty(set()), build=reg, n=6 + i) for i in range(3))]
    sel = select_runs(runs, last=3)
    assert [r.id for r in sel.shown] == ["b0", "b1", "b2"]
    assert [r.id for r in sel.regressions] == ["r0", "r1", "r2"]
    text, doc = quality_report(sel.shown, data_source="d", question_set_key="S",
                               regressions=sel.regressions)
    assert "3 deliberate-regression run(s) left out" in text
    assert doc["regressions_left_out"] == ["r0", "r1", "r2"]
    # the newest runs are a regression: compared with the build it was made of
    sel = select_runs(runs, compare_builds=True)
    assert [r.id for r in sel.baseline] == ["b0", "b1", "b2"]
    assert [r.id for r in sel.candidate] == ["r0", "r1", "r2"] and sel.regressions == []
    text, _doc = quality_report(sel.shown, data_source="d", question_set_key="S",
                                baseline=sel.baseline, candidate=sel.candidate)
    assert "the candidate is a **deliberate regression**" in text
    # a later build is compared with B2, not with the regression
    runs += [run(f"c{i}", thirty(set()), build="B3", n=9 + i) for i in range(3)]
    sel = select_runs(runs, compare_builds=True)
    assert {r.build for r in sel.baseline} == {"B2"} and {r.build for r in sel.candidate} == {"B3"}
    assert [r.id for r in sel.regressions] == ["r0", "r1", "r2"]
    text, _doc = quality_report(sel.shown, data_source="d", question_set_key="S",
                                baseline=sel.baseline, candidate=sel.candidate)
    assert "deliberate regression**" not in text
    # runs named are read as named
    assert [r.id for r in select_runs(runs, ids=["r"]).shown] == ["r0", "r1", "r2"]


def test_the_confidence_shown_says_what_is_not_known():
    runs = [run(f"r{i}", thirty({"q07"}), n=i) for i in range(2)]
    for k in ("q00", "q01"):
        runs[1].items[k].detail = {"confidence": {"level": "high", "score": 0.9}}
    text, _doc = quality_report(runs, data_source="d", question_set_key="S")
    assert "shown as high: right 2 of 2" in text
    assert "not known: 58 answer(s) (runs recorded before build D84" in text
