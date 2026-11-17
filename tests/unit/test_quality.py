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
