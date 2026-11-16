"""A business question, answered: retrieve → generate → guard → execute → repair →
narrate (Phase 4C).

``ask`` is pure orchestration — no metadata session: the caller loads the index
and guard, and records the result (``record_answer``). Statuses:

* ``answered`` — a table (the result) and a narrative (P4-2); the SQL is kept for
  audit and shown only on request;
* ``clarify`` — the question needs the user's choice (P4-4: clarify always rather
  than guess), with the question to ask and options;
* ``cannot_answer`` — the data is not there, with the reason;
* ``failed`` — no valid query within ``query.max_repair_attempts`` repairs, or the
  model gave no usable answer; every attempt and why it failed is kept.

A repair continues the same conversation: the model's previous answer, then what
went wrong — the guard's findings, or the database's error, or the timeout.

**Confidence** is explained, not only a number: the mean certainty of the meanings
the answer relies on (what the model listed in ``uses`` and the columns it shows),
less a little for every repair, a join that rests on a model hint, a result cut at
the row cap, and each assumption. ``high`` ≥ 0.8, ``medium`` ≥ 0.6, else ``low``.
A heuristic until Phase 6 calibrates it from the golden set and SME decisions.

**Best guess (D61).** A clarification carries the option the model would have
chosen and its SQL; the SQL is checked by the guard but **not run** — the user sees
no number before choosing. When the user then picks that very option, ``preset``
answers with the stored best guess, without a new generation call; if it fails,
the usual repairs follow in the same conversation.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from gaia.core.config import TargetConfig
from gaia.llm.base import LLMClient, Message
from gaia.llm.structured import CallRecord, StructuredOutputError, complete_structured
from gaia.query.answerer import narrate
from gaia.query.executor import ExecutionResult, execute
from gaia.query.planner import (
    GENERATION_MAJOR,
    Clarification,
    SqlPlan,
    check_plan,
    generation_messages,
    repair_message,
)
from gaia.query.prompts import query_prompt
from gaia.query.retrieval import (
    IndexedColumn,
    RetrievalIndex,
    RetrievalSettings,
    SchemaContext,
    VectorHit,
    retrieve,
)
from gaia.query.validator import GuardResult, SqlGuardBase

__all__ = ["Answer", "AskSettings", "Attempt", "Confidence", "ask"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AskSettings:
    model: str
    row_limit: int = 1000
    max_repair_attempts: int = 2
    rows_to_model: int = 30
    thinking_generation: bool = False
    thinking_repair: bool = False
    thinking_answer: bool = False
    max_tokens: int | None = None
    temperature: float | None = 0.1
    raw_values_to_model: bool = False  # llm.locality local, or the dev override
    statement_timeout_s: int | None = None


@dataclass
class Attempt:
    number: int
    sql: str
    guard: GuardResult
    execution: ExecutionResult | None = None
    problem: str | None = None
    calls: list[CallRecord] = field(default_factory=list)  # the call that produced this SQL

    @property
    def ok(self) -> bool:
        return self.guard.ok and self.execution is not None and self.execution.ok


@dataclass
class Confidence:
    level: str  # high | medium | low
    score: float
    reasons: list[str]


@dataclass
class Answer:
    question: str
    status: str  # answered | clarify | cannot_answer | failed
    context: SchemaContext | None = None
    plan: SqlPlan | None = None
    narrative: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_cap_reached: bool = False
    uncertain: list[str] = field(default_factory=list)
    confidence: Confidence | None = None
    clarification: Clarification | None = None
    best_guess: GuardResult | None = None  # the guard on the default option's SQL (D61)
    preset_used: bool = False
    personal_columns: set[int] = field(default_factory=set)  # result columns of personal data
    reason: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)  # every model call, in order
    notes: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def sql(self) -> str | None:
        ok = [a for a in self.attempts if a.ok]
        return ok[-1].guard.sql if ok else None

    @property
    def interpretation(self) -> str:
        return self.plan.interpretation if self.plan else ""

    @property
    def assumptions(self) -> list[str]:
        return list(self.plan.assumptions) if self.plan else []


def _column(index: RetrievalIndex, ref: str) -> tuple[str, IndexedColumn] | None:
    table, _, column = ref.strip().rpartition(".")
    obj = index.obj(table) if table else None
    col = obj.column(column) if obj is not None else None
    return (f"{obj.key}.{col.name}", col) if obj is not None and col is not None else None


def _relied_on(index: RetrievalIndex, plan: SqlPlan, guard: GuardResult
               ) -> list[tuple[str, IndexedColumn]]:
    refs = list(plan.uses) + [base for _n, base in guard.outputs if base]
    seen, out = set(), []
    for ref in refs:
        hit = _column(index, ref)
        if hit is not None and hit[0].lower() not in seen:
            seen.add(hit[0].lower())
            out.append(hit)
    return out


def _describe(key: str, c: IndexedColumn) -> str:
    if c.certainty == "none":
        return f"{key}: no meaning yet (known by its name only)"
    score = f", {c.score:.2f}" if c.score is not None else ""
    return f"{key}: {c.business_name} ({c.band}{score})"


def confidence(relied: list[tuple[str, IndexedColumn]], attempts: list[Attempt],
               plan: SqlPlan, capped: bool) -> Confidence:
    scores, reasons = [], []
    for key, c in relied:
        if c.status == "sme_reviewed":
            scores.append(1.0)
        elif c.certainty == "certain":
            scores.append(max(c.score or 0.9, 0.8))
        elif c.certainty == "uncertain":
            scores.append(c.score if c.score is not None else 0.5)
            reasons.append(f"relies on an unconfirmed meaning: {_describe(key, c)}")
        else:
            scores.append(0.3)
            reasons.append(f"relies on a column with no meaning yet: {key}")
    score = sum(scores) / len(scores) if scores else 0.5
    if not scores:
        reasons.append("the columns relied on are not known to Gaia's catalog")
    repairs = len(attempts) - 1
    if repairs:
        score -= 0.05 * repairs
        reasons.append(f"the query needed {repairs} repair(s)")
    final = attempts[-1].guard if attempts else None
    hinted = [j for j in (final.joins if final else []) if j.kind.startswith("hint")]
    if hinted:
        score -= 0.05
        reasons.append("a join rests on a tested model hint, not a declared key")
    if capped:
        score -= 0.05
        reasons.append("the result stops at the row limit")
    if plan.assumptions:
        score -= min(0.15, 0.05 * len(plan.assumptions))
        reasons.append(f"{len(plan.assumptions)} assumption(s) made")
    score = max(0.0, min(1.0, score))
    level = "high" if score >= 0.8 else "medium" if score >= 0.6 else "low"
    return Confidence(level, round(score, 2), reasons)


def _problem(guard: GuardResult, execution: ExecutionResult | None, timeout_s: int | None
             ) -> str:
    if not guard.ok:
        return "Your query was rejected by the SQL guard:\n" + "\n".join(
            f"- {v.code}: {v.message}" for v in guard.errors)
    assert execution is not None
    if execution.status == "timeout":
        limit = f" ({timeout_s} s)" if timeout_s else ""
        return (f"Your query ran longer than the time limit{limit} and was cancelled. Make it "
                "cheaper: filter by date first, aggregate before joining, avoid needless joins.")
    return f"Your query failed on the database ({execution.status}): {execution.error}"


def ask(question: str, *, index: RetrievalIndex, guard: SqlGuardBase, target: TargetConfig,
        llm: LLMClient, settings: AskSettings, today: date,
        clarifications: list[tuple[str, str]] | None = None,
        vector_hits: list[VectorHit] | None = None,
        retrieval: RetrievalSettings | None = None, notes: list[str] | None = None,
        preset: SqlPlan | None = None, history: list[tuple[str, str]] | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None) -> Answer:
    """``history``: earlier turns of a conversation, (question, what was computed), for
    follow-up questions. ``progress(stage, data)`` is told each stage as it starts:
    ``retrieval``, ``generation``, ``checking``, ``running``, ``repairing``, ``result``,
    ``narrating`` (the API streams them, 4E)."""
    started = time.perf_counter()
    answer = Answer(question=question, status="failed", notes=list(notes or []))

    def tell(stage: str, **data: Any) -> None:
        if progress is not None:
            try:
                progress(stage, data)
            except Exception:  # a listener that fails must not fail the answer
                logger.warning("progress listener failed at %s", stage, exc_info=True)

    previous = [q for q, _c in (history or [])[-1:]]
    search = " ".join([question] + [a for _q, a in clarifications or []] + previous)
    tell("retrieval")
    ctx = retrieve(index, search, vector_hits=vector_hits, settings=retrieval,
                   notes=answer.notes)
    answer.context = ctx
    answer.notes = ctx.notes
    tell("retrieved", objects=ctx.tables)
    if not ctx.objects:
        answer.status = "clarify"
        answer.clarification = Clarification(
            question=f"I found nothing in {index.data_source} that matches this question. "
                     "Could you say it with the business terms you use for the data?")
        return _done(answer, started)

    messages = generation_messages(ctx, question, today, clarifications, history)
    prompt = query_prompt("generate_sql", GENERATION_MAJOR)

    def call(conversation: list[Message], purpose: str, thinking: bool
             ) -> tuple[SqlPlan | None, str | None, list[CallRecord]]:
        try:
            out = complete_structured(
                llm, conversation, SqlPlan, purpose=purpose, model=settings.model,
                prompt_version=prompt.version, thinking=thinking,
                max_tokens=settings.max_tokens, temperature=settings.temperature,
                validation_retries=1, checker=check_plan)
        except StructuredOutputError as exc:
            return None, str(exc), exc.calls
        raw = out.calls[-1].content or out.value.model_dump_json()
        return out.value, raw, out.calls

    if preset is not None:
        plan, raw, calls = preset, preset.model_dump_json(), []
        answer.preset_used = True
        answer.notes.append("answered with the best guess stored with the clarification")
    else:
        tell("generation")
        plan, raw, calls = call(messages, "sql_generation", settings.thinking_generation)
        answer.calls += calls
    if plan is None:
        answer.reason = f"the model gave no usable answer: {raw}"
        return _done(answer, started)
    answer.plan = plan

    conversation = list(messages)
    for number in range(1, settings.max_repair_attempts + 2):
        if plan.decision != "answer":
            answer.status = "clarify" if plan.decision == "clarify" else "cannot_answer"
            answer.clarification, answer.reason = plan.clarification, plan.reason
            if plan.decision == "clarify" and (plan.sql or "").strip():
                answer.best_guess = guard.check(plan.sql or "")  # checked, never run
            return _done(answer, started)
        tell("checking", attempt=number)
        attempt = Attempt(number, plan.sql or "", guard.check(plan.sql or ""), calls=calls)
        answer.attempts.append(attempt)
        if attempt.guard.ok:
            tell("running", attempt=number)
            attempt.execution = execute(target, attempt.guard, row_limit=settings.row_limit,
                                        statement_timeout_s=settings.statement_timeout_s)
        if attempt.ok:
            break
        attempt.problem = _problem(attempt.guard, attempt.execution,
                                   settings.statement_timeout_s)
        if number > settings.max_repair_attempts:
            answer.reason = (f"no valid query after {settings.max_repair_attempts} "
                             f"repair(s); last problem: {attempt.problem}")
            return _done(answer, started)
        tell("repairing", attempt=number, problem=attempt.problem.splitlines()[0][:200])
        conversation += [Message("assistant", raw or ""),
                         Message("user", repair_message(attempt.problem))]
        new_plan, raw, calls = call(conversation, "sql_repair", settings.thinking_repair)
        answer.calls += calls
        if new_plan is None:
            answer.reason = f"the repair gave no usable answer: {raw}"
            return _done(answer, started)
        plan = answer.plan = new_plan

    final = answer.attempts[-1]
    execution = final.execution
    assert execution is not None
    answer.status = "answered"
    answer.columns, answer.rows = execution.columns, execution.rows
    answer.row_cap_reached = execution.row_cap_reached
    relied = _relied_on(index, plan, final.guard)
    answer.uncertain = [_describe(k, c) for k, c in relied if c.certainty != "certain"]
    personal = {f"{o.key}.{c.name}".lower() for o in index.objects.values()
                for c in o.columns if c.personal}
    answer.personal_columns = {i for i, (_n, base) in enumerate(final.guard.outputs)
                               if base and base.lower() in personal}
    hidden = set() if settings.raw_values_to_model else set(answer.personal_columns)
    tell("result", columns=answer.columns, rows=answer.rows,
         row_cap_reached=answer.row_cap_reached)
    tell("narrating")
    answer.narrative, narr_calls, error = narrate(
        llm, model=settings.model, question=question, interpretation=plan.interpretation,
        assumptions=plan.assumptions, uncertain=answer.uncertain, columns=answer.columns,
        rows=answer.rows, hidden=hidden, capped=answer.row_cap_reached,
        rows_to_model=settings.rows_to_model, thinking=settings.thinking_answer,
        max_tokens=settings.max_tokens, temperature=settings.temperature)
    answer.calls += narr_calls
    if error:
        answer.notes.append(f"narrative written without the model: {error}")
    answer.confidence = confidence(relied, answer.attempts, plan, answer.row_cap_reached)
    return _done(answer, started)


def _done(answer: Answer, started: float) -> Answer:
    answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return answer


def answer_dict(a: Answer, *, with_sql: bool = False) -> dict[str, Any]:
    """JSON-ready answer (the API of 4E will return this shape)."""
    from gaia.query.answerer import _cell

    doc: dict[str, Any] = {
        "question": a.question, "status": a.status, "narrative": a.narrative,
        "columns": a.columns, "rows": [[_cell(v) for v in r] for r in a.rows],
        "row_cap_reached": a.row_cap_reached, "interpretation": a.interpretation,
        "assumptions": a.assumptions, "uncertain": a.uncertain,
        "confidence": a.confidence.__dict__ if a.confidence else None,
        "clarification": a.clarification.model_dump() if a.clarification else None,
        "best_guess": ({"interpretation": a.interpretation, "assumptions": a.assumptions,
                        "valid": a.best_guess.ok} if a.best_guess else None),
        "reason": a.reason, "attempts": len(a.attempts), "notes": a.notes,
        "elapsed_ms": a.elapsed_ms,
        "llm": {"calls": len(a.calls),
                "prompt_tokens": sum(c.usage.prompt_tokens for c in a.calls),
                "completion_tokens": sum(c.usage.completion_tokens for c in a.calls)},
    }
    if with_sql:
        doc["sql"] = a.sql
        doc["attempt_log"] = [{"sql": t.guard.sql or t.sql, "problem": t.problem}
                              for t in a.attempts]
    return json.loads(json.dumps(doc, default=str))
