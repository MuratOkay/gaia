"""Golden questions drafted by Gaia, for a person to correct (Phase 7B, P6-6, P7-3; D97).

A golden set is the yardstick of everything Phase 6 measures, and a database without one has
no quality number at all — ``nge_small``, the first target, among them (P4-1). Writing thirty
questions with reference SQL by hand from an unfamiliar schema takes a day; correcting thirty
good drafts takes an hour (P6-6). So Gaia drafts, and a person decides:

* **Where to look** — *seeds*: the tables most connected (declared joins) and largest, spread
  over the schemas; each seed with its direct neighbours is one small context, so the model
  sees a few related objects at a time, as retrieval would show them.
* **What to write** — per seed a few questions of different kinds (``draft_golden`` prompt):
  business wording; everything the numbers depend on stated; a reference SQL in the target's
  dialect; the objects it reads; ``identify_by``/``rank_by`` for the comparison; at most one
  ambiguous question with the clarified reading; personal data avoided (or marked).
* **Checked before a person sees it** — every reference goes through the same guard and
  executor as any answer (read-only, the target's limits): refused, failed and empty ones are
  kept but marked with the problem; the objects are the ones the guard saw it read (not the
  model's list); duplicates (the same words or the same SQL) are dropped; a preview of the
  result comes with each — personal data as shapes.
* **Never counted as drafted** — every question carries ``status: draft``; ``load_golden``
  skips them until the person removes the line (or writes ``accepted``). A wrong reference
  would poison every later measurement: a disputed draft is deleted, not guessed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from gaia.core.config import TargetConfig
from gaia.llm.base import LLMClient, Message
from gaia.llm.structured import StructuredOutputError, complete_structured
from gaia.query.executor import execute
from gaia.query.planner import DIALECT_NAMES
from gaia.query.prompts import query_prompt
from gaia.query.retrieval import RetrievalIndex, RetrievalSettings, context_of
from gaia.query.validator import SqlGuardBase
from gaia.semantics.evidence import shape

__all__ = ["Draft", "DraftQuestion", "DraftSet", "KINDS", "choose_seeds", "draft_questions",
           "drafts_yaml"]

KINDS = ("lookup", "aggregate", "join", "period", "ranking", "ratio", "ambiguous")
PROMPT_MAJOR = 1
PREVIEW_ROWS = 5


class DraftQuestion(BaseModel):
    question: str
    expect: Literal["answer", "clarify"] = "answer"
    kind: str = "aggregate"
    objects: list[str] = Field(default_factory=list)
    sql: str = ""
    clarified: str | None = None
    identify_by: list[str] = Field(default_factory=list)
    rank_by: str | None = None
    order_matters: bool = False
    personal: bool = False
    notes: str | None = None


class DraftSet(BaseModel):
    questions: list[DraftQuestion] = Field(default_factory=list)


def check_drafts(d: DraftSet) -> list[str]:
    problems = []
    if not d.questions:
        problems.append("questions is empty")
    for i, q in enumerate(d.questions):
        if not q.question.strip():
            problems.append(f"questions[{i}].question is empty")
        if not q.sql.strip():
            problems.append(f"questions[{i}].sql is empty: every draft needs its reference")
        if q.expect == "clarify" and not (q.clarified or "").strip():
            problems.append(f"questions[{i}] is 'clarify' without `clarified`")
    return problems


@dataclass
class Draft:
    q: DraftQuestion
    seed: str
    status: str = "ok"  # ok | empty | refused | failed
    problem: str | None = None
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: int = 0
    preview: list[list[str]] = field(default_factory=list)


# -- where to look ------------------------------------------------------------------------------


def choose_seeds(index: RetrievalIndex, n: int = 10, *, neighbours: int = 4
                 ) -> list[list[str]]:
    """Up to ``n`` small contexts: a seed table and its directly joined objects (largest
    first). Seeds are the most connected and largest tables, taken so that every schema gets
    one before any gets a second, and skipped when already inside an earlier context."""
    degree: dict[str, int] = {}
    links: dict[str, set[str]] = {}
    for e in index.catalog.join_edges():
        a, b = index.obj(e.from_table), index.obj(e.to_table)
        if a is None or b is None or a.key == b.key:
            continue
        for x, y in ((a.key, b.key), (b.key, a.key)):
            degree[x] = degree.get(x, 0) + 1
            links.setdefault(x, set()).add(y)

    def weight(key: str) -> float:
        o = index.objects[key]
        return 2.0 * degree.get(key, 0) + math.log10((o.rows or 0) + 1) + (
            1.0 if o.business_name else 0.0)

    reasoned = any(o.business_name for o in index.objects.values())

    def useful(key: str) -> bool:
        """Not a table nothing joins to and with nothing to ask about: no column with a usable
        meaning beyond an identifier (empty or constant columns have none: set aside as
        unused)."""
        o = index.objects[key]
        return not reasoned or degree.get(key, 0) > 0 or any(
            c.reasoned and c.role != "identifier" for c in o.columns)

    tables = sorted((k for k, o in index.objects.items() if o.kind == "table" and useful(k)),
                    key=lambda k: (-weight(k), k))
    by_schema: dict[str, list[str]] = {}
    for k in tables:
        by_schema.setdefault(index.objects[k].schema, []).append(k)
    order: list[str] = []
    while any(by_schema.values()):  # round-robin over schemas, strongest first in each
        for schema in sorted(by_schema, key=lambda s: -weight(by_schema[s][0])
                             if by_schema[s] else 0):
            if by_schema[schema]:
                order.append(by_schema[schema].pop(0))
    seeds: list[list[str]] = []
    covered: set[str] = set()
    for key in order:
        if len(seeds) >= n:
            break
        if key in covered:
            continue
        near = sorted(links.get(key, set()),
                      key=lambda k: (-(index.objects[k].rows or 0), k))[:neighbours]
        seeds.append([key, *near])
        covered |= {key, *near}
    return seeds


# -- what to write, and checking it ---------------------------------------------------------------


def _messages(ctx_text: str, dialect: str, today: date, wanted: int,
              already: list[str]) -> list[Message]:
    parts = [f"Dialect: {DIALECT_NAMES.get(dialect, dialect)}", f"Today: {today.isoformat()}",
             f"Write {wanted} questions.", "", "Database context:", ctx_text]
    if already:
        parts += ["", "Questions already written (do not repeat them):",
                  *(f"- {q}" for q in already[-40:])]
    return [Message("system", query_prompt("draft_golden", PROMPT_MAJOR).text),
            Message("user", "\n".join(parts))]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _personal_outputs(index: RetrievalIndex, outputs: list[tuple[str, str | None]]
                      ) -> set[str]:
    out = set()
    for name, base in outputs:
        if not base:
            continue
        table, _, column = base.rpartition(".")
        obj = index.obj(table)
        col = obj.column(column) if obj is not None else None
        if col is not None and col.personal:
            out.add(name.lower())
    return out


def _check(d: Draft, index: RetrievalIndex, guard: SqlGuardBase, target: TargetConfig,
           row_limit: int, timeout_s: int | None) -> None:
    g = guard.check(d.q.sql)
    if not g.ok:
        d.status = "refused"
        d.problem = "; ".join(str(v) for v in g.errors)[:300]
        return
    d.tables = list(g.tables)
    r = execute(target, g, row_limit=row_limit, statement_timeout_s=timeout_s)
    if not r.ok:
        d.status, d.problem = "failed", f"{r.status}: {r.error}"[:300]
        return
    d.columns, d.rows = list(r.columns), len(r.rows)
    if not r.rows:
        d.status, d.problem = "empty", "the reference returns no rows"
    personal = _personal_outputs(index, g.outputs)
    for row in r.rows[:PREVIEW_ROWS]:
        d.preview.append([shape(str(v)) if c.lower() in personal and v is not None
                          else str(v)[:40] for c, v in zip(r.columns, row, strict=False)])


def draft_questions(index: RetrievalIndex, *, guard: SqlGuardBase, target: TargetConfig,
                    llm: LLMClient, model: str, today: date, seeds: list[list[str]],
                    per_seed: int = 3, thinking: bool = True, max_tokens: int | None = None,
                    temperature: float | None = 0.3, row_limit: int = 1000,
                    timeout_s: int | None = None,
                    retrieval: RetrievalSettings | None = None,
                    progress: Callable[[str, list[Draft], str | None], None] | None = None
                    ) -> tuple[list[Draft], dict[str, Any]]:
    """Draft ``per_seed`` questions per seed context, check each reference, drop duplicates.
    Returns the drafts and the cost (calls, tokens) — a seed whose call fails is reported
    and skipped."""
    prompt = query_prompt("draft_golden", PROMPT_MAJOR)
    out: list[Draft] = []
    seen_q: set[str] = set()
    seen_sql: set[str] = set()
    cost = {"calls": 0, "tokens": 0, "seeds": len(seeds), "failed_seeds": 0, "duplicates": 0}
    for objects in seeds:
        ctx = context_of(index, objects, settings=retrieval, note=objects[0])
        messages = _messages(ctx.render(), index.dialect, today, per_seed,
                             [d.q.question for d in out])
        try:
            result = complete_structured(
                llm, messages, DraftSet, purpose="golden_draft", model=model,
                prompt_version=prompt.version, thinking=thinking, max_tokens=max_tokens,
                temperature=temperature, validation_retries=1, checker=check_drafts)
        except StructuredOutputError as exc:
            cost["failed_seeds"] += 1
            calls = exc.calls
            error: str | None = str(exc)[:200]
            made: list[Draft] = []
        else:
            calls, error = result.calls, None
            made = []
            for q in result.value.questions:
                key_q, key_sql = _norm(q.question), _norm(q.sql)
                if key_q in seen_q or key_sql in seen_sql:
                    cost["duplicates"] += 1
                    continue
                seen_q.add(key_q)
                seen_sql.add(key_sql)
                d = Draft(q, objects[0])
                _check(d, index, guard, target, row_limit, timeout_s)
                made.append(d)
        cost["calls"] += len(calls)
        cost["tokens"] += sum(c.usage.prompt_tokens + c.usage.completion_tokens for c in calls)
        out += made
        if progress:
            progress(objects[0], made, error)
    return out, cost


# -- the file a person corrects -------------------------------------------------------------------


class _Literal(str):
    """A block scalar in YAML (``|``) — SQL stays readable."""


def _literal(dumper: yaml.SafeDumper, value: _Literal) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


yaml.SafeDumper.add_representer(_Literal, _literal)


def drafts_yaml(drafts: list[Draft], *, data_source: str, as_of: date, model: str,
                build: str, cost: dict[str, Any]) -> str:
    """The golden file to correct: every question ``status: draft``, the checks' findings
    and a preview of each reference's result in a ``draft`` block the loader ignores."""
    ok = sum(1 for d in drafts if d.status == "ok")
    head = [
        f"# Golden questions for {data_source}, DRAFTED by Gaia ({model}, build {build}) —",
        "# for a person to correct (Phase 7B, P6-6). Nothing here is measured until it is.",
        "#",
        "# For each question: read the question as a business user would; check the reference",
        "# SQL and its preview (the `draft` block: what the guard saw it read, how many rows,",
        "# the first rows — personal data as shapes); correct the wording, the SQL, `objects`,",
        "# `identify_by`, `rank_by`; then delete the line `status: draft`. Delete a question",
        "# you cannot vouch for: a wrong reference poisons every later measurement.",
        "# `as_of` is the day relative periods are read from: set it to the data's \"today\".",
        "#",
        f"# {len(drafts)} draft(s): {ok} ran and returned rows; "
        f"{sum(1 for d in drafts if d.status == 'empty')} returned none; "
        f"{sum(1 for d in drafts if d.status in ('refused', 'failed'))} were refused or failed "
        f"(kept, marked). {cost.get('duplicates', 0)} duplicate(s) dropped. "
        f"{cost.get('calls', 0)} model call(s), {cost.get('tokens', 0):,} tokens.",
    ]
    questions = []
    for i, d in enumerate(drafts, start=1):
        q = d.q
        entry: dict[str, Any] = {"id": f"d{i:02d}", "status": "draft",
                                 "question": q.question.strip(), "expect": q.expect}
        if q.clarified:
            entry["clarified"] = q.clarified.strip()
        entry["objects"] = d.tables or list(q.objects)
        for key in ("identify_by",):
            if getattr(q, key):
                entry[key] = list(getattr(q, key))
        if q.rank_by:
            entry["rank_by"] = q.rank_by
        if q.order_matters:
            entry["order_matters"] = True
        if q.personal:
            entry["personal"] = True
        if q.notes:
            entry["notes"] = q.notes.strip()
        entry["sql"] = _Literal(q.sql.strip() + "\n")
        entry["draft"] = {"kind": q.kind if q.kind in KINDS else "other", "seed": d.seed,
                          "check": d.status, **({"problem": d.problem} if d.problem else {}),
                          "rows": d.rows, "columns": d.columns, "preview": d.preview}
        questions.append(entry)
    body = yaml.safe_dump({"data_source": data_source, "as_of": as_of.isoformat(),
                           "questions": questions}, sort_keys=False, allow_unicode=True,
                          width=100, default_flow_style=None)
    return "\n".join(head) + "\n" + body
