"""7D (D101): a candidate's meaning judged against the SME's decision, the settings that reach
a registered model, the probe's checks, trial runs kept out of the ordinary report, and a
trial's result in words."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from gaia.core.config import LLMConfig, load_settings
from gaia.evaluation.net import TRIAL_MARK, is_trial
from gaia.evaluation.quality import ItemView, RunView, select_runs
from gaia.llm.base import LLMResponse, Usage
from gaia.models.registry import ModelEntry, llm_config_of, run_probe
from gaia.models.trial import judge_column, name_words, trial_lines


def _m(name, role="measure", entity=None, unit=None, synonyms=()):
    return NS(business_name=name, role=role, entity=entity, unit=unit, synonyms=list(synonyms))


def test_a_meaning_judged_against_the_sme_decision():
    decided = _m("Invoice amount", unit="TRY")
    assert judge_column(_m("Invoice amount", unit="try"), decided) == "right"
    assert judge_column(_m("Amount of the invoice"), decided) == "right"  # unit unknown: fine
    assert judge_column(_m("Total", synonyms=["invoice amount"]), decided) == "right"
    assert judge_column(_m("Gross total"), decided) == "partly"  # under half the words
    assert judge_column(_m("Invoice amount", unit="EUR"), decided) == "partly"
    assert judge_column(_m("Invoice amount", role="identifier"), decided) == "wrong"
    assert judge_column(None, decided) == "wrong"
    assert name_words("The Customer ID code") == {"customer"}


def _settings():
    return load_settings()


def test_the_settings_that_reach_a_registered_model(monkeypatch):
    settings = _settings()
    cfg = ModelEntry(name=settings.llm.model, origin="config", base_url="", model_id="",
                     locality="remote", status="config")
    assert llm_config_of(cfg, settings) is settings.llm
    monkeypatch.setenv("QWEN_KEY", "sk-qwen")
    qwen = ModelEntry(name="qwen", origin="admin", base_url="https://gw.example/v1",
                      model_id="qwen-max", locality="remote", status="verified",
                      key_env="QWEN_KEY",
                      options={"concurrency": 4, "thinking": {"semantics": False},
                               "price_in_per_million": 0.4})
    got = llm_config_of(qwen, settings)
    assert isinstance(got, LLMConfig)
    assert (got.base_url, got.model, got.api_key, got.concurrency) == (
        "https://gw.example/v1", "qwen-max", "sk-qwen", 4)
    assert got.thinking.semantics is False
    assert got.thinking.critique == settings.llm.thinking.critique  # the rest kept
    assert got.replay_dir is None and got.locality == "remote"
    local = ModelEntry(name="llama", origin="admin", base_url="http://localhost:11434/v1",
                       model_id="llama3", locality="local", status="verified")
    assert llm_config_of(local, settings).api_key == "local"  # Ollama asks for none
    monkeypatch.delenv("QWEN_KEY")
    assert llm_config_of(qwen, settings).api_key == ""  # the probe says it is not set


class _Scripted:
    def __init__(self, json_ok=True, reasoning="because"):
        self.json_ok, self.reasoning = json_ok, reasoning

    def complete(self, messages, *, json_mode=False, thinking=False, max_tokens=None,
                 temperature=None):
        if not json_mode:
            content = "ready"
        elif self.json_ok:
            content = json.dumps({"business_name": "Order total", "purpose": "x",
                                  "confidence": 0.8})
        else:
            content = "Sure! Here is the JSON you asked for."
        return LLMResponse(content=content, model="m-1", finish_reason="stop",
                           usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                           reasoning=self.reasoning if thinking else None)


def test_the_probe_needs_a_plain_answer_and_json_mode():
    checks = {c["name"]: c for c in run_probe(_Scripted())}
    assert all(c["ok"] for c in checks.values())
    assert checks["plain"]["detail"] == "'ready' from m-1"
    assert checks["json"]["detail"] == "business_name='Order total'"
    bad = {c["name"]: c for c in run_probe(_Scripted(json_ok=False, reasoning=""))}
    assert bad["plain"]["ok"] and not bad["json"]["ok"] and "JSONDecodeError" in bad["json"][
        "detail"]
    assert not bad["thinking"]["ok"]

    class Down:
        def complete(self, *a, **k):
            raise ConnectionError("Name or service not known")

    down = run_probe(Down())
    assert [c["ok"] for c in down] == [False, False, False]
    assert "ConnectionError" in down[0]["detail"]
    assert down[1]["detail"] == down[2]["detail"] == "not tried: the plain answer failed"


def _run(rid, build, day, correct=True):
    from datetime import UTC, datetime

    items = {"g01": ItemView(key="g01", correct=correct, tokens=100, seconds=1.0)}
    return RunView(id=rid, started_at=datetime(2026, 11, day, tzinfo=UTC), build=build,
                   model="m", mode="live", items=items, set_key="s")


def test_trial_runs_stay_out_of_the_ordinary_report():
    assert is_trial(f"D101{TRIAL_MARK}qwen") and not is_trial("D101")
    runs = [_run("a", "D101", 1), _run("b", f"D101{TRIAL_MARK}qwen", 2, False),
            _run("c", "D101", 3)]
    sel = select_runs(runs, set_key="s", last=5)
    assert [r.id for r in sel.shown] == ["a", "c"] and [r.id for r in sel.trials] == ["b"]
    picked = select_runs(runs, ids=["b"])  # asked for by id: shown
    assert [r.id for r in picked.shown] == ["b"] and picked.trials == []


def test_a_trial_in_words():
    result = {
        "model": "qwen", "model_id": "qwen-max", "locality": "remote", "purpose": "both",
        "answering": {"against": "deepseek-flash", "verdict": "fails", "difference": -0.25,
                      "low": -0.5, "high": -0.05, "questions": 4, "breaks": ["g01"],
                      "fixes": [],
                      "current_runs": {"correct": 12, "scored": 12, "tokens_per_run": 9000,
                                       "seconds_per_question": 2.0},
                      "candidate_runs": {"correct": 9, "scored": 12, "tokens_per_run": 7000,
                                         "seconds_per_question": 1.5}},
        "reasoning": {"against": "deepseek-flash", "tables": ["finance.invoices"],
                      "candidate": {"n": 4, "right": 2, "partly": 1, "wrong": 1,
                                    "share": 0.625, "low": 0.2, "high": 0.9,
                                    "tokens_per_table": 1400, "seconds_per_table": 3,
                                    "misses": [{"verdict": "wrong",
                                                "column": "finance.invoices.status",
                                                "decided": "Invoice status (code)",
                                                "answer": "Status (attribute)"}]}},
        "preview": {"tables": 25, "sme_decisions_kept": 7, "calibrated": [],
                    "reasoning": {"tokens": 35000, "nights": 1, "hours": 0.1, "cost": 0.01},
                    "answering": {"tokens_per_golden_run": 7000,
                                  "seconds_per_question": 1.5}},
    }
    text = "\n".join(trial_lines(result))
    assert "answering against deepseek-flash: **fails**, -25.0 points" in text
    assert "broken: g01" in text and "candidate: 9 of 12 right" in text
    assert "candidate (qwen): right 2, partly 1, wrong 1 of 4 = 62%" in text
    assert "wrong: finance.invoices.status — decided Invoice status (code)" in text
    assert "re-reasoning 25 table(s): ~35,000 tokens, ~1 night(s)" in text
    assert "starts uncalibrated" in text and "kept: 7 SME decision(s)" in text
    skipped = trial_lines({"model": "x", "purpose": "reasoning",
                           "reasoning": {"skipped": "no SME decision to compare with"}})
    assert "meanings: not measured — no SME decision" in skipped[1]
