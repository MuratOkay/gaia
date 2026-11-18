"""Golden questions (Phase 4, P4-1): the file format; the runner and the result
comparison come with 4D.

A golden file (``infra/pg-target/golden.yaml``) names its data source and the date
relative periods are measured from (``as_of``), and lists questions: ``expect:
answer`` with a reference ``sql`` (right = the result holds the reference's values),
or ``expect: clarify`` (right = a clarification or "cannot answer", never a number) —
optionally with the user's answer to the clarification (``clarified``) and the
reference for the clarified question (``sql``), which also judges Gaia's best guess
(D61). ``identify_by`` and ``rank_by`` tell the comparison (``query.compare``) which
columns are interchangeable identifiers and how a top-N list is ranked.

Phase 7B (D97): ``personal: true`` marks a question that filters, groups or computes with
personal data — asked as someone who is not a PII processor it must be refused (a golden run
as a person, ``--as``); ``status: draft`` marks a question drafted by ``make golden-draft`` and
not yet corrected by a person — it is skipped (never run, never counted) until the line goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

__all__ = ["GoldenQuestion", "GoldenSet", "load_golden"]


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    question: str
    expect: str  # answer | clarify
    sql: str | None = None
    objects: list[str] = field(default_factory=list)
    order_matters: bool = False
    requires: list[str] = field(default_factory=list)
    notes: str | None = None
    clarified: str | None = None
    identify_by: list[str] = field(default_factory=list)
    rank_by: str | None = None
    personal: bool = False  # needs personal data: refused for someone who is no PII processor

    @property
    def has_reference(self) -> bool:
        return self.sql is not None


@dataclass(frozen=True)
class GoldenSet:
    data_source: str
    as_of: date
    questions: list[GoldenQuestion]
    drafts: int = 0  # questions still marked ``status: draft`` (skipped)


def load_golden(path: str | Path, *, include_drafts: bool = False) -> GoldenSet:
    """The questions of a golden file; those still ``status: draft`` are left out unless
    ``include_drafts`` (a draft is not a measurement until a person has corrected it)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    questions = []
    drafts = 0
    for q in doc["questions"]:
        status = str(q.get("status") or "").strip().lower()
        if status not in ("", "draft", "accepted"):
            raise ValueError(f"{q.get('id')}: status is 'draft' or nothing (or 'accepted')")
        if status == "draft" and not include_drafts:
            drafts += 1
            continue
        if q["expect"] not in ("answer", "clarify"):
            raise ValueError(f"{q['id']}: expect must be 'answer' or 'clarify'")
        if q["expect"] == "answer" and not q.get("sql"):
            raise ValueError(f"{q['id']}: an 'answer' question needs a reference sql")
        if q["expect"] == "clarify" and bool(q.get("sql")) != bool(q.get("clarified")):
            raise ValueError(f"{q['id']}: a clarify question has both `clarified` and `sql`, "
                             "or neither")
        questions.append(GoldenQuestion(
            id=q["id"], question=q["question"].strip(), expect=q["expect"],
            sql=(q.get("sql") or "").strip() or None, objects=list(q.get("objects") or []),
            order_matters=bool(q.get("order_matters")), requires=list(q.get("requires") or []),
            notes=q.get("notes"), clarified=q.get("clarified"),
            identify_by=list(q.get("identify_by") or []), rank_by=q.get("rank_by"),
            personal=bool(q.get("personal"))))
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("golden question ids must be unique")
    as_of = doc["as_of"]
    return GoldenSet(doc["data_source"], as_of if isinstance(as_of, date)
                     else date.fromisoformat(str(as_of)), questions, drafts)
