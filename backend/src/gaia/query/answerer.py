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


def _sort_key(visible: list[int]):
    """A total order over result rows by their visible columns: numbers by value, then text
    and dates by their text, empty last — the same on every server and in every run."""
    def cell(v: Any) -> tuple:
        if v is None:
            return (2, "")
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            return (0, v)
        return (1, v.isoformat() if isinstance(v, (date, datetime)) else str(v))

    return lambda row: tuple(cell(row[i]) for i in visible)


def result_for_model(columns: list[str], rows: list[tuple], hidden: set[int],
                     limit: int, ordered: bool = True) -> dict[str, Any]:
    """At most ``limit`` rows for the model. When the query does not order its rows and there
    are more than ``limit``, which rows come first is the database plan's choice — another
    server (or another day) would show the model other rows (D90: g09's 40 stores on the
    owner's host). Such a result is sorted by its visible columns before it is cut, and says
    so: the rows shown are the first by their values, not a ranking."""
    doc_extra: dict[str, Any] = {}
    if not ordered and len(rows) > limit:
        visible = [i for i in range(len(columns)) if i not in hidden]
        rows = sorted(rows, key=_sort_key(visible))
        doc_extra["rows_shown_are"] = ("the first by the columns' values: the query has no "
                                       "ORDER BY, so they are not the top or bottom of anything")
    shown = [[HIDDEN if i in hidden else _cell(v) for i, v in enumerate(r)]
             for r in rows[:limit]]
    return {"columns": columns, "row_count": len(rows), "rows_shown": len(shown),
            "rows": shown, **doc_extra}


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
            thinking: bool, max_tokens: int | None, temperature: float | None,
            ordered: bool = True) -> tuple[str, list[CallRecord], str | None]:
    """(narrative, calls, error). On failure the narrative is ``fallback_narrative``."""
    result = result_for_model(columns, rows, hidden, rows_to_model, ordered)
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
