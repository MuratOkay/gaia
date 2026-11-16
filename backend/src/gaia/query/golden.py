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

    @property
    def has_reference(self) -> bool:
        return self.sql is not None


@dataclass(frozen=True)
class GoldenSet:
    data_source: str
    as_of: date
    questions: list[GoldenQuestion]


def load_golden(path: str | Path) -> GoldenSet:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    questions = []
    for q in doc["questions"]:
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
            identify_by=list(q.get("identify_by") or []), rank_by=q.get("rank_by")))
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("golden question ids must be unique")
    as_of = doc["as_of"]
    return GoldenSet(doc["data_source"], as_of if isinstance(as_of, date)
                     else date.fromisoformat(str(as_of)), questions)
