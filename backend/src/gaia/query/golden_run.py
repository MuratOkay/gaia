"""Running a golden set through the whole pipeline (Phase 4D).

For every question, what a user would live through, scored against the reference:

* ``expect: answer`` — the first answer is scored (*correct first*). A clarification
  instead is *over-clarification*: the question is then re-asked with the answer
  "exactly as the question says", and that answer scored too.
* ``expect: clarify`` — asking back (or "cannot answer") is right, a number is not
  (*answered directly*). Where the golden file has the user's answer (``clarified``)
  and a reference: Gaia's **best guess** is run here — only here, never for a user —
  and scored (*best guess correct*, D61's shadow measurement); then the question is
  re-asked with the user's answer and that answer scored (*correct after
  clarification*).

Headline numbers (Phase 4 acceptance), over every question with a reference and
counting the answer the user finally gets: **executable** (the final query ran) and
**correct** (its result is the reference's, ``query.compare``). Then the clarification
policy (how often Gaia asked when it should, and when it should not), the best-guess
accuracy, repairs, calls, tokens and time.

The model is whatever ``llm`` is; a ``RecordingLLMClient`` around it makes a run
resumable and a re-run free (every answer is kept).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from gaia.core.config import TargetConfig
from gaia.llm.base import LLMClient
from gaia.query.ask import Answer, AskSettings, ask
from gaia.query.compare import Table, compare
from gaia.query.executor import execute
from gaia.query.golden import GoldenQuestion, GoldenSet
from gaia.query.retrieval import RetrievalIndex, RetrievalSettings, VectorHit
from gaia.query.validator import SqlGuardBase

__all__ = ["AS_STATED", "GoldenOutcome", "answer_detail", "reference_table", "run_golden",
           "summarise"]

AS_STATED = "Exactly as the question says; it states what it means."
_EXTEND = 50  # rows fetched beyond a top-N reference's limit, to see its ties


@dataclass
class GoldenOutcome:
    id: str
    question: str
    expect: str
    first_status: str = ""
    correct_first: bool | None = None  # the first answer (expect answer, or answered directly)
    over_clarified: bool = False
    answered_directly: bool = False  # a clarify question answered with a number
    best_guess: str = "none"  # none | invalid | wrong | correct | not scored
    final_status: str = ""
    executable: bool | None = None  # only for questions with a reference
    correct: bool | None = None
    reason: str = ""  # why the final answer is not correct
    repairs: int = 0
    calls: int = 0
    tokens: int = 0
    seconds: float = 0.0
    interpretation: str = ""
    clarification: str | None = None
    options: list[str] = field(default_factory=list)
    default: int | None = None
    ambiguity: str | None = None
    sql: str | None = None
    # what the final answer was made of (D84): the context's objects, the expected objects it
    # lacked, the interpretation, assumptions, the clarification given, the confidence shown
    detail: dict[str, Any] = field(default_factory=dict)
    answers: list[Answer] = field(default_factory=list, repr=False)


def _extended(sql: str, dialect: str) -> str | None:
    """The reference with its row limit raised, to see the rows tied at the cut."""
    tree = sqlglot.parse_one(sql, read=dialect)
    limit = tree.args.get("limit")
    count = limit.expression if isinstance(limit, exp.Limit) else None
    if not isinstance(count, exp.Literal) or count.is_string:
        return None
    limit.set("expression", exp.Literal.number(int(count.name) + _EXTEND))
    return tree.sql(dialect=dialect)


def reference_table(q: GoldenQuestion, guard: SqlGuardBase, target: TargetConfig,
                    row_limit: int) -> tuple[Table, list[tuple] | None]:
    """The reference result, through the same guard and executor as any answer."""
    g = guard.check(q.sql or "")
    if not g.ok:
        raise ValueError(f"{q.id}: the reference is refused by the guard: "
                         + "; ".join(str(v) for v in g.errors))
    r = execute(target, g, row_limit=row_limit)
    if not r.ok:
        raise ValueError(f"{q.id}: the reference failed: {r.error}")
    extended = None
    if q.rank_by:
        more = _extended(q.sql or "", guard.read)
        if more:
            ge = guard.check(more)
            re_ = execute(target, ge, row_limit=row_limit) if ge.ok else None
            extended = re_.rows if re_ is not None and re_.ok else None
    return Table(r.columns, r.rows), extended


def _score(q: GoldenQuestion, answer: Answer, ref: tuple[Table, list[tuple] | None] | None
           ) -> tuple[bool, str]:
    if ref is None or answer.status != "answered":
        return False, f"status {answer.status}" + (f": {answer.reason}" if answer.reason else "")
    table, extended = ref
    c = compare(table, Table(answer.columns, answer.rows), identify_by=q.identify_by,
                rank_by=q.rank_by, order_matters=q.order_matters, extended=extended)
    return c.match, "" if c.match else c.reason


def answer_detail(q: GoldenQuestion, answer: Answer, *, clarified: list[str] | None = None
                  ) -> dict[str, Any]:
    """What an answer was made of, to read why it was wrong (D84): whether the context held
    every object the reference needs (``missing``: a retrieval miss) or the model misread a
    context that held them all."""
    objects = answer.context.tables if answer.context is not None else []
    out: dict[str, Any] = {
        "objects": objects,
        "missing": [o for o in q.objects if o not in objects] if objects else list(q.objects),
        "interpretation": answer.interpretation,
        "assumptions": answer.assumptions,
    }
    if clarified:
        out["clarification"] = clarified
    if answer.confidence is not None:
        c = answer.confidence
        out["confidence"] = {"level": c.level, "score": c.score}
        if c.probability is not None:
            out["confidence"] |= {"probability": c.probability, "calibrator": c.calibrator}
    return out


def _one(q: GoldenQuestion, *, golden: GoldenSet, ref, run: Callable[..., Answer],
         guard: SqlGuardBase, target: TargetConfig, row_limit: int) -> GoldenOutcome:
    started = time.perf_counter()
    out = GoldenOutcome(q.id, q.question, q.expect)
    first = run(q.question)
    out.answers.append(first)
    out.first_status = first.status
    out.interpretation = first.interpretation
    final = first
    given: list[str] | None = None  # the clarification the final answer was given
    if first.status == "clarify" and first.clarification is not None:
        c = first.clarification
        out.clarification, out.options = c.question, list(c.options)
        out.default, out.ambiguity = c.default, c.ambiguity

    if q.expect == "answer":
        if first.status == "answered":
            out.correct_first, out.reason = _score(q, first, ref)
        else:
            out.correct_first = False
            if first.status == "clarify":
                out.over_clarified = True
                given = [out.clarification or "", AS_STATED]
                final = run(q.question, [(given[0], given[1])])
                out.answers.append(final)
    else:
        out.best_guess = "not scored"  # until there is a reference to score it against
        if first.status == "answered":
            out.answered_directly = True
            if ref is not None:
                out.correct_first, out.reason = _score(q, first, ref)
        elif first.status == "clarify" and ref is not None:
            if first.best_guess is None:
                out.best_guess = "none"
            elif not first.best_guess.ok:
                out.best_guess = "invalid"
            else:
                result = execute(target, first.best_guess, row_limit=row_limit)
                guess = Answer(q.question, "answered" if result.ok else "failed",
                               columns=result.columns, rows=result.rows, reason=result.error)
                ok, _why = _score(q, guess, ref)
                out.best_guess = "correct" if ok else "wrong"
            given = [out.clarification or "", q.clarified or ""]
            final = run(q.question, [(given[0], given[1])])
            out.answers.append(final)

    out.final_status = final.status
    out.interpretation = final.interpretation or out.interpretation
    if ref is not None:
        out.executable = final.status == "answered"
        if final is first and q.expect == "answer" and first.status == "answered":
            out.correct = out.correct_first
        else:
            out.correct, reason = _score(q, final, ref)
            out.reason = "" if out.correct else reason
        out.sql = final.sql
    out.detail = answer_detail(q, final, clarified=given if final is not first else None)
    for a in out.answers:
        out.repairs += max(0, len(a.attempts) - 1)
        out.calls += len(a.calls)
        out.tokens += sum(c.usage.prompt_tokens + c.usage.completion_tokens for c in a.calls)
    out.seconds = round(time.perf_counter() - started, 1)
    return out


def run_golden(golden: GoldenSet, *, index: RetrievalIndex, guard: SqlGuardBase,
               target: TargetConfig, llm: LLMClient, settings: AskSettings,
               retrieval: RetrievalSettings | None = None,
               vectors: Callable[[str], list[VectorHit] | None] | None = None,
               only: list[str] | None = None, workers: int = 1,
               progress: Callable[[GoldenOutcome], None] | None = None) -> list[GoldenOutcome]:
    questions = [q for q in golden.questions if not only or q.id in only]
    refs: dict[str, Any] = {}
    for q in questions:
        refs[q.id] = reference_table(q, guard, target, settings.row_limit) if q.sql else None

    def run_for(q: GoldenQuestion) -> Callable[..., Answer]:
        hits = vectors(q.question) if vectors else None

        def run(question: str, clarifications: list[tuple[str, str]] | None = None) -> Answer:
            return ask(question, index=index, guard=guard, target=target, llm=llm,
                       settings=settings, today=golden.as_of, clarifications=clarifications,
                       vector_hits=hits, retrieval=retrieval)
        return run

    def task(q: GoldenQuestion) -> GoldenOutcome:
        out = _one(q, golden=golden, ref=refs[q.id], run=run_for(q), guard=guard,
                   target=target, row_limit=settings.row_limit)
        if progress:
            progress(out)
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(task, questions))


def _share(items: list[bool]) -> float | None:
    return sum(items) / len(items) if items else None


def summarise(outcomes: list[GoldenOutcome]) -> dict[str, Any]:
    scored = [o for o in outcomes if o.correct is not None]
    answer_q = [o for o in outcomes if o.expect == "answer"]
    clarify_q = [o for o in outcomes if o.expect == "clarify"]
    guessed = [o for o in clarify_q if o.best_guess in ("correct", "wrong", "invalid", "none")
               and o.first_status == "clarify" and o.correct is not None]
    return {
        "questions": len(outcomes),
        "with_reference": len(scored),
        "executable": _share([bool(o.executable) for o in scored]),
        "correct": _share([bool(o.correct) for o in scored]),
        "correct_first": _share([bool(o.correct_first) for o in answer_q]),
        "over_clarified": sum(o.over_clarified for o in answer_q),
        "answer_questions": len(answer_q),
        "clarified_when_needed": _share([o.first_status in ("clarify", "cannot_answer")
                                         for o in clarify_q]),
        "answered_directly": sum(o.answered_directly for o in clarify_q),
        "clarify_questions": len(clarify_q),
        "best_guess_correct": _share([o.best_guess == "correct" for o in guessed]),
        "best_guess_scored": len(guessed),
        "repairs": sum(o.repairs for o in outcomes),
        "calls": sum(o.calls for o in outcomes),
        "tokens": sum(o.tokens for o in outcomes),
        "seconds": round(sum(o.seconds for o in outcomes), 1),
    }


def report_markdown(golden: GoldenSet, outcomes: list[GoldenOutcome], summary: dict[str, Any],
                    model: str, build: str, provenance: str | None = None) -> str:
    def pct(x: float | None) -> str:
        return "—" if x is None else f"{x:.0%}"

    s = summary
    lines = [
        f"# Golden run — {golden.data_source}", "",
        f"Model `{model}` · build `{build}` · as of {golden.as_of} · {s['questions']} "
        f"question(s), {s['with_reference']} with a reference.", "",
        *([f"**Answers:** {provenance}.", ""] if provenance else []),
        "## Phase 4 acceptance", "",
        "| Criterion | Value | Target |", "|---|---|---|",
        f"| Executable SQL (the final answer ran) | {pct(s['executable'])} | ≥ 80 % |",
        f"| Correct result (= the reference) | {pct(s['correct'])} | ≥ 70 % (trial) |",
        "", "## Clarification (P4-4, D61)", "",
        f"* answer questions answered right the first time: {pct(s['correct_first'])}; "
        f"clarified although clear: {s['over_clarified']} of {s['answer_questions']}",
        f"* clarify questions asked back: {pct(s['clarified_when_needed'])}; answered with a "
        f"number instead: {s['answered_directly']} of {s['clarify_questions']}",
        f"* best guess = the user's clarified answer: {pct(s['best_guess_correct'])} of "
        f"{s['best_guess_scored']} (the measurement for answering without asking)",
        "", f"Cost: {s['calls']} model call(s), {s['tokens']:,} tokens, {s['repairs']} "
        f"repair(s), {s['seconds']} s.", "",
        "## Questions", "",
        "| Id | Expect | First | Final | Correct | Best guess | Repairs | Calls | s | Why not |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        mark = {True: "yes", False: "**no**", None: "—"}[o.correct]
        why = re.sub(r"\s+", " ", o.reason)[:90]
        lines.append(f"| {o.id} | {o.expect} | {o.first_status} | {o.final_status} | {mark} | "
                     f"{o.best_guess if o.expect == 'clarify' else ''} | {o.repairs} | "
                     f"{o.calls} | {o.seconds} | {why} |")
    asked = [o for o in outcomes if o.clarification]
    if asked:
        lines += ["", "## What Gaia asked", ""]
        for o in asked:
            guess = (f" — Gaia would choose {o.default + 1}" if o.default is not None else "")
            lines.append(f"* **{o.id}** ({o.ambiguity or '?'}): {o.clarification}{guess}")
            for i, opt in enumerate(o.options, start=1):
                lines.append(f"  {i}. {opt}")
    return "\n".join(lines) + "\n"
