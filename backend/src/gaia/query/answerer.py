"""The narrative next to the result table (Phase 4C; P4-2: table + narrative).

The model sees the question, the interpretation, the assumptions, the
unconfirmed meanings, and the result — at most ``rows_to_model`` rows. A remote
model never sees values of columns that show personal data as is (D24): the
guard knows which base column each output column shows (``GuardResult.outputs``)
and the index knows which columns hold personal data; those values are replaced
by ``[hidden]``. The user's table is not masked: the user may read the data.

When the narrative call fails, a plain narrative is written without the model,
so a failed explanation never costs the user the answer.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from gaia.llm.base import LLMClient, Message
from gaia.llm.structured import CallRecord, StructuredOutputError, complete_structured
from gaia.query.prompts import query_prompt

__all__ = ["Narrative", "fallback_narrative", "narrate", "result_for_model"]

HIDDEN = "[hidden]"


class Narrative(BaseModel):
    narrative: str


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value) if value != value.to_integral_value() else int(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def result_for_model(columns: list[str], rows: list[tuple], hidden: set[int],
                     limit: int) -> dict[str, Any]:
    shown = [[HIDDEN if i in hidden else _cell(v) for i, v in enumerate(r)]
             for r in rows[:limit]]
    return {"columns": columns, "row_count": len(rows), "rows_shown": len(shown),
            "rows": shown}


def _messages(question: str, interpretation: str, assumptions: list[str],
              uncertain: list[str], result: dict[str, Any], capped: bool) -> list[Message]:
    doc = {
        "question": question,
        "interpretation": interpretation,
        "assumptions": assumptions,
        "unconfirmed_meanings": uncertain,
        "result": result,
        "result_is_capped": capped,  # the table stopped at the row cap: there may be more
    }
    return [Message("system", query_prompt("answer").text),
            Message("user", "Answer this (json):\n" + json.dumps(doc, ensure_ascii=False,
                                                                  indent=1))]


def narrate(llm: LLMClient, *, model: str, question: str, interpretation: str,
            assumptions: list[str], uncertain: list[str], columns: list[str],
            rows: list[tuple], hidden: set[int], capped: bool, rows_to_model: int,
            thinking: bool, max_tokens: int | None, temperature: float | None
            ) -> tuple[str, list[CallRecord], str | None]:
    """(narrative, calls, error). On failure the narrative is ``fallback_narrative``."""
    result = result_for_model(columns, rows, hidden, rows_to_model)
    prompt = query_prompt("answer")
    try:
        out = complete_structured(
            llm, _messages(question, interpretation, assumptions, uncertain, result, capped),
            Narrative, purpose="answer", model=model, prompt_version=prompt.version,
            thinking=thinking, max_tokens=max_tokens, temperature=temperature,
            validation_retries=1,
            checker=lambda v: [] if v.narrative.strip() else ["narrative is empty"])
    except StructuredOutputError as exc:
        return fallback_narrative(interpretation, len(rows), capped), exc.calls, str(exc)
    return out.value.narrative.strip(), out.calls, None


def fallback_narrative(interpretation: str, row_count: int, capped: bool) -> str:
    what = interpretation.rstrip(".") if interpretation else "The query"
    if row_count == 0:
        return f"{what}: no data matched."
    more = " (the table stops at the row limit; there may be more)" if capped else ""
    return f"{what}: {row_count} row(s){more}."
