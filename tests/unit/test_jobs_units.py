"""7C (D98): when schedules fall due, when work must stop, and comparisons across an edited
question set."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaia.evaluation.quality import ItemView, RunView, compare, shared_questions
from gaia.jobs.queue import JobRefused
from gaia.jobs.schedules import next_due, parse_when
from gaia.jobs.signal import MeteredLLM, StopSignal
from gaia.llm.base import LLMResponse, Usage
from gaia.tools.jobs import parse_params

LOCAL = datetime.now().astimezone().tzinfo


def _local(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL)


def test_when_a_schedule_falls_due():
    assert parse_when("daily 02:00") == ("daily", None, "02:00")
    assert parse_when("Weekly Sunday 04:30") == ("weekly", 6, "04:30")
    for bad in ("daily 2am", "weekly 04:00", "monthly 01:00", "daily 24:00"):
        with pytest.raises(JobRefused):
            parse_when(bad)
    wed_noon = _local(2026, 11, 18, 12, 0)  # a Wednesday
    assert next_due("daily", None, "02:00", wed_noon) == _local(2026, 11, 19, 2, 0).astimezone(UTC)
    assert next_due("daily", None, "13:00", wed_noon) == _local(2026, 11, 18, 13, 0).astimezone(UTC)
    assert next_due("weekly", 0, "06:00", wed_noon) == _local(2026, 11, 23, 6, 0).astimezone(UTC)
    # strictly after: at the very minute, the next one
    at = _local(2026, 11, 23, 6, 0)
    assert next_due("weekly", 0, "06:00", at) == _local(2026, 11, 30, 6, 0).astimezone(UTC)


def test_when_work_must_stop():
    assert StopSignal().reason() is None
    sig = StopSignal(budget_tokens=1000)
    llm = MeteredLLM(type("M", (), {"model": "m", "complete": lambda self, m, **k: LLMResponse(
        content="{}", model="m", usage=Usage(prompt_tokens=400, completion_tokens=200))})(), sig)
    llm.complete([])
    assert (sig.used_tokens, sig.calls, sig.reason()) == (600, 1, None)
    llm.complete([])
    assert sig.reason() == "the token budget of 1,000 is spent"
    sig.cancelled = True
    assert sig.reason() == "cancelled" and sig.stopped == "the token budget of 1,000 is spent"
    night = StopSignal(window="22:00-06:00", clock=lambda: datetime(2026, 11, 18, 23, 0))
    assert night.reason() is None
    morning = StopSignal(window="22:00-06:00", clock=lambda: datetime(2026, 11, 19, 6, 30))
    assert morning.reason() == "the run window 22:00-06:00 closed"


def test_job_parameters_from_the_command_line():
    assert parse_params(["budget=500000", "ignore_window=1", "file=x.yaml", "no_vectors=no"]) \
        == {"budget": 500000, "ignore_window": True, "file": "x.yaml", "no_vectors": False}
    with pytest.raises(JobRefused):
        parse_params(["budget"])


def _run(i, keys, texts=None, right=True, set_key="A"):
    t0 = datetime(2026, 11, 18, tzinfo=UTC) + timedelta(hours=i)
    return RunView(f"r{i}", t0, "B", "m", "fresh", None, {
        k: ItemView(k, "answer", right, question=(texts or {}).get(k, f"q {k}"))
        for k in keys}, set_key=set_key)


def test_a_comparison_reads_the_questions_both_sides_share_with_one_wording():
    base = [_run(i, ["a", "b", "h19"], set_key="A") for i in range(3)]
    cand = [_run(10 + i, ["a", "b", "h19", "c"], {"h19": "reworded"}, set_key="B")
            for i in range(3)]
    keys, reworded, one_side = shared_questions(base, cand)
    assert (keys, reworded, one_side) == (["a", "b"], ["h19"], 1)
    c = compare(base, cand)
    assert c.questions == 2 and c.reworded == ["h19"] and c.only_one_side == 1
    # older runs without the wording recorded compare by id
    old = [RunView(f"o{i}", datetime(2026, 1, 1, tzinfo=UTC), "B", "m", "imported", None,
                   {"a": ItemView("a", "answer", True)}) for i in range(3)]
    assert shared_questions(old, cand)[0] == ["a"]


def test_a_comparison_of_builds_reaches_back_across_a_new_question_set():
    from gaia.evaluation.quality import select_runs

    older = [_run(i, ["a", "b"], set_key="A") for i in range(3)]
    for r in older:
        r.build = "D92"
    newer = [_run(10 + i, ["a", "b", "h01"], set_key="B") for i in range(3)]
    for r in newer:
        r.build = "D97"
    sel = select_runs([*older, *newer], compare_builds=True)
    assert [r.id for r in sel.candidate] == ["r10", "r11", "r12"]
    assert [r.id for r in sel.baseline] == ["r0", "r1", "r2"]
    assert compare(sel.baseline, sel.candidate).questions == 2


def test_a_job_builds_the_real_clients_when_none_is_given():
    """D98: the scripted tests pass their own model and embedder; the worker's defaults are
    the configured ones — built here, so a wrong import is caught before a night's run."""
    from gaia.core.config import AppSection, EmbeddingsConfig, LLMConfig, Settings
    from gaia.jobs.kinds import JobContext, JobDeps
    from gaia.llm.base import LLMError

    settings = Settings(app=AppSection(default_target="demo_pg"), targets={},
                        llm=LLMConfig(model="m", api_key=""),
                        embeddings=EmbeddingsConfig(base_url="http://127.0.0.1:9", model="e",
                                                    dimensions=1024))
    ctx = JobContext(settings, None, None, StopSignal(), JobDeps(), None)
    embedder = ctx.embedder()
    assert hasattr(embedder, "embed") and hasattr(embedder, "close")
    embedder.close()
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        ctx.llm()
