"""The judge: does an answer mean what the reference means? (PHASES 3C "Metric")

One call per (condition, table), all columns at once, thinking off (a
classification task, ``llm.thinking.classification``). The judge sees real names
in the reference — also for name-blind conditions, whose answers were produced
without them — and never the condition, the score or the evidence.

Caveat kept in the report: the judge shares a model family with the reasoner;
the owner's 20 % audit exists to catch that (``audit_sample``).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from gaia.llm.base import LLMClient, Message
from gaia.llm.structured import StructuredOutputError, complete_structured
from gaia.semantics.prompts import load_prompt
from gaia.semantics.schemas import _coverage_errors

__all__ = ["ColumnJudgement", "JudgeItem", "TableJudgement", "audit_sample", "judge_table",
           "judge_prompt"]


class ColumnJudgement(BaseModel):
    column: str = Field(min_length=1)
    meaning: Literal["yes", "partial", "no"]
    role_ok: bool | None = None
    note: str = ""

    @field_validator("meaning", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("note", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Any:
        return "" if v is None else v


class TableJudgement(BaseModel):
    columns: list[ColumnJudgement]


@dataclass
class JudgeItem:
    column: str  # real name
    data_type: str
    reference: dict[str, Any]  # {"kind": names|label|ground_truth, ...}
    answer: dict[str, Any]  # business_name, purpose, role, entity, unit


def judge_prompt():
    return load_prompt("judge_meaning", 1)


def judge_messages(table: str, items: list[JudgeItem]) -> list[Message]:
    doc = {
        "table": table,
        "columns": [
            {"column": i.column, "type": i.data_type, "reference": i.reference,
             "answer": i.answer}
            for i in items
        ],
    }
    user = "Table to judge (json):\n" + json.dumps(doc, indent=2, ensure_ascii=False)
    return [Message("system", judge_prompt().text), Message("user", user)]


def judge_table(
    llm: LLMClient, table: str, items: list[JudgeItem], *, model: str, thinking: bool,
    max_tokens: int | None,
) -> tuple[TableJudgement | None, str | None, list]:
    """(judgement, error, call records)."""
    expected = [i.column for i in items]
    try:
        result = complete_structured(
            llm,
            judge_messages(table, items),
            TableJudgement,
            purpose="judge",
            model=model,
            prompt_version=judge_prompt().version,
            thinking=thinking,
            max_tokens=max_tokens,
            temperature=0.0,
            validation_retries=1,
            checker=lambda v: _coverage_errors([c.column for c in v.columns], expected),
        )
    except StructuredOutputError as exc:
        return None, str(exc), exc.calls
    return result.value, None, result.calls


def audit_sample(keys: list[Any], share: float = 0.2, seed: int = 42) -> list[Any]:
    """A seeded random share of the judged columns for the owner's audit."""
    ordered = sorted(keys, key=str)
    k = max(1, round(len(ordered) * share)) if ordered else 0
    return sorted(random.Random(seed).sample(ordered, k), key=str)  # noqa: S311
