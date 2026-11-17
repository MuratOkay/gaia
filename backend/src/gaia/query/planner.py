"""What the SQL generator must return, and the messages it is sent (Phase 4C).

One call plans and writes the query: the model decides whether to **answer**,
**clarify** (P4-4: always, when a guess would be needed) or say it **cannot
answer**; states what the query computes and what it assumed; writes the SQL;
and lists the columns it relies on, so the answer can disclose the uncertain
meanings among them (R2). One call rather than a planner call and a generator
call: at the gateway's speed every call is tens of seconds.

Message layout, for the provider's prefix cache: the static system prompt, then
one user message — dialect, today, the schema context, earlier clarifications,
the question. A repair appends the model's answer and the problem to the same
conversation, so the prefix stays identical.

Prompt generation 2 (D61): a clarification also carries the model's **best
guess** — the option it would choose (``default``), the kind of ambiguity, and the
SQL, interpretation and assumptions of that option — so the choice can be recorded
against the user's and its accuracy measured before Gaia ever answers such a
question without asking.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from gaia.llm.base import Message
from gaia.query.prompts import query_prompt
from gaia.query.retrieval import SchemaContext

__all__ = [
    "Clarification",
    "SqlPlan",
    "check_plan",
    "present",
    "shown_order",
    "generation_messages",
    "repair_message",
]

DIALECT_NAMES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL",
                 "mssql": "SQL Server (T-SQL)"}


# prompt generation of generate_sql: v1 no best guess; v2 best guess (D61); v3 follow-up
# questions read with the conversation so far (Phase 4E)
GENERATION_MAJOR = 3
AMBIGUITY_KINDS = ("measure", "period", "population", "entity", "ranking", "scope", "other")


class Clarification(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)
    default: int | None = None  # the option Gaia would choose (D61)
    ambiguity: str | None = None  # AMBIGUITY_KINDS

    @property
    def default_text(self) -> str | None:
        if self.default is None or not 0 <= self.default < len(self.options):
            return None
        return self.options[self.default]


def shown_order(question: str, options: list[str]) -> list[int]:
    """The order a clarification's options are shown in: a permutation of the model's
    order, seeded by the question and the options (5.0, T5).

    On the host the model put its own choice first in 13 of 13 clarifications; shown in
    that order, the first option *is* Gaia's choice, which D61 keeps from the user so the
    agreement it measures is not pulled towards it. Seeded rather than random: a replayed
    run shows the same order, and tests are deterministic — while the position of Gaia's
    choice no longer depends on anything the model decides."""
    order = list(range(len(options)))
    if len(order) < 2:
        return order
    seed = hashlib.sha256("\x1f".join([question, *options]).encode("utf-8")).digest()
    random.Random(int.from_bytes(seed[:8], "big")).shuffle(order)  # noqa: S311 - not security
    return order


def present(clarification: Clarification, question: str) -> tuple[Clarification, list[int]]:
    """A copy of ``clarification`` with its options in the shown order and ``default``
    pointing at the same option; and the model's index of each shown option."""
    order = shown_order(question, clarification.options)
    shown = clarification.model_copy(deep=True)
    shown.options = [clarification.options[i] for i in order]
    if clarification.default is not None and 0 <= clarification.default < len(order):
        shown.default = order.index(clarification.default)
    return shown, order


class SqlPlan(BaseModel):
    decision: Literal["answer", "clarify", "cannot_answer"]
    interpretation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    sql: str | None = None
    uses: list[str] = Field(default_factory=list)
    clarification: Clarification | None = None
    reason: str | None = None


def check_plan(plan: SqlPlan) -> list[str]:
    """Semantic checks the JSON schema cannot express (fed back on a retry)."""
    problems = []
    if plan.decision == "answer":
        if not (plan.sql or "").strip():
            problems.append("decision is 'answer' but sql is empty")
        if not plan.interpretation.strip():
            problems.append("decision is 'answer' but interpretation is empty")
    elif plan.decision == "clarify":
        c = plan.clarification
        if c is None or not c.question.strip():
            problems.append("decision is 'clarify' but clarification.question is empty")
        else:
            if c.default is not None and not 0 <= c.default < len(c.options):
                problems.append(f"clarification.default {c.default} is not the position of "
                                f"one of the {len(c.options)} options")
            if (plan.sql or "").strip() and c.default is None:
                problems.append("a clarification with sql needs clarification.default: the "
                                "option that sql answers")
            if c.ambiguity is not None and c.ambiguity not in AMBIGUITY_KINDS:
                problems.append(f"clarification.ambiguity must be one of "
                                f"{', '.join(AMBIGUITY_KINDS)}")
    elif not (plan.reason or "").strip():
        problems.append("decision is 'cannot_answer' but reason is empty")
    return problems


def generation_messages(ctx: SchemaContext, question: str, today: date,
                        clarifications: list[tuple[str, str]] | None = None,
                        history: list[tuple[str, str]] | None = None) -> list[Message]:
    """``history``: earlier turns of the conversation, (question, what was computed)."""
    parts = [
        f"Dialect: {DIALECT_NAMES.get(ctx.dialect, ctx.dialect)}",
        f"Today: {today.isoformat()}",
        "",
        "Database context:",
        ctx.render(),
    ]
    if history:
        parts += ["", "Conversation so far:"]
        for asked, computed in history:
            parts.append(f"- User: {asked}")
            if computed:
                parts.append(f"  Computed: {computed}")
    if clarifications:
        # Worded exactly as in build D65 (97 % correct). The D68 rule "a clarification
        # settles what it answered" went with three golden runs at 90 %, the misses mostly
        # answers *after* a clarification; withdrawn until measured (D72, OWNER_VERIFICATION
        # §24b). T6's real fix — the follow-up carries its question — is code, and stays.
        parts += ["", "Earlier clarifications:"]
        for asked, answered in clarifications:
            parts.append(f"- Asked: {asked}" if asked else "- Asked: (a clarification)")
            parts.append(f"  User: {answered}")
    parts += ["", f"Question: {question.strip()}"]
    return [Message("system", query_prompt("generate_sql", GENERATION_MAJOR).text),
            Message("user", "\n".join(parts))]


def repair_message(problem: str) -> str:
    return (
        f"{problem}\n\nReturn the whole JSON object again with a corrected sql. Use only the "
        "objects, columns and joins of the context."
    )
