"""Replay LLM provider and review-queue rules (Phase 3B)."""

from __future__ import annotations

import json

import pytest

from gaia.core.config import ProfilingConfig
from gaia.llm.base import LLMError, Message
from gaia.llm.replay import ReplayLLMClient, pending_requests, request_key
from gaia.semantics.bundle import ColumnFacts
from gaia.semantics.evidence import Ledger
from gaia.semantics.review import importance, review_reasons
from gaia.semantics.schemas import ColumnCritique
from gaia.semantics.scoring import ColumnScore

MESSAGES = [Message("system", "You are a senior data analyst"),
            Message("user", 'Table bundle (json):\n{\n  "table": "dbo.orders"\n}')]


def test_replay_writes_requests_then_answers(tmp_path):
    client = ReplayLLMClient(tmp_path)
    with pytest.raises(LLMError, match="awaiting replay response"):
        client.complete(MESSAGES, json_mode=True)
    pending = pending_requests(tmp_path)
    assert [(p["purpose"], p["table"]) for p in pending] == [("semantics", "dbo.orders")]
    key = pending[0]["key"]
    assert key == request_key(MESSAGES, {"json_mode": True, "thinking": False, "max_tokens": None})
    request = json.loads((tmp_path / "requests" / f"{key}.json").read_text())
    assert request["messages"][1]["content"].startswith("Table bundle")
    assert (tmp_path / "requests" / f"{key}.md").exists()

    (tmp_path / "responses" / f"{key}.json").write_text('{"ok": true}')
    response = client.complete(MESSAGES, json_mode=True)
    assert response.content == '{"ok": true}' and response.model == "replay"
    assert pending_requests(tmp_path) == []


def _score(value: float, band: str) -> ColumnScore:
    return ColumnScore(raw_score=value, calibrated_score=value, band=band, status="draft",
                       components=[], weights_version="heuristic-v2", calibrator="identity",
                       critique_assessed=True)


def test_review_rules():
    bands = ProfilingConfig()
    low, mid, ok = _score(0.40, "abstain"), _score(0.60, "deep_profile"), _score(0.85, "accepted")
    assert review_reasons(ok, ledger=None, critique=None, exhausted=True, unused=False,
                          bands=bands) == []
    assert review_reasons(low, ledger=None, critique=None, exhausted=False, unused=False,
                          bands=bands) == ["abstain"]  # abstain is queued even if not exhausted
    assert review_reasons(mid, ledger=None, critique=None, exhausted=False, unused=False,
                          bands=bands) == []  # more evidence can still come
    ledger = Ledger()
    ledger.add("detector", False, 0.5, "detector")
    doubt = ColumnCritique(name="c", verdict="doubtful")
    assert review_reasons(mid, ledger=ledger, critique=doubt, exhausted=True, unused=False,
                          bands=bands) == ["evidence_exhausted", "contradiction",
                                           "critique_disagrees"]
    assert review_reasons(low, ledger=None, critique=None, exhausted=True, unused=True,
                          bands=bands) == []  # unused columns never reach an SME


def test_importance_orders_what_matters():
    plain = ColumnFacts(name="note", data_type="nvarchar(200)", nullable=True)
    key = ColumnFacts(name="id", data_type="int", nullable=False, refs={"pk:PK"},
                      foreign_keys=["fk:x"])
    assert importance(key, 24_000_000, 6) > importance(plain, 24_000_000, 6)
    assert importance(plain, 24_000_000, 0) > importance(plain, 100, 0)
    assert importance(plain, 0, 0) == 0.05
