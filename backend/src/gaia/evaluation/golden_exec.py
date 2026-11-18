"""One golden run, recorded — shared by ``make golden`` and the drift watch (Phase 4D, 6A; 7C).

Moved out of ``gaia.tools.golden`` unchanged in what it does (D98): the tool parses its
arguments and prints; the drift watch (``gaia.jobs.kinds``) runs the same code inside the
worker, with the worker's engine and its own model client. See ``gaia.tools.golden`` for what
a run does and records. New in 7C: the run's summary says which model the provider reports
answered (``answered_by``), so a watch can see a name move on to another model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gaia
from gaia.access.policy import OPERATOR, build_access_guard, policy_for_login, restrict_index
from gaia.auth.service import cli_actor
from gaia.core.config import Settings
from gaia.db.repositories.query_store import QueryStore
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.evaluation.store import (
    git_commit,
    meanings_state,
    question_set_key,
    record_golden,
    run_mode,
)
from gaia.query.golden import GoldenSet, load_golden
from gaia.query.golden_run import report_markdown, run_golden, summarise, withheld_from
from gaia.query.retrieval_store import load_retrieval_index
from gaia.tools.ask import answer_calibrator_for, ask_settings, llm_client

__all__ = ["GoldenExecution", "GoldenOptions", "GoldenRefused", "ROOT", "execute_golden",
           "resolve_path"]

ROOT = Path(__file__).resolve().parents[4]


def resolve_path(text: str | Path) -> Path:
    p = Path(text)
    return p if p.is_absolute() or p.exists() else ROOT / p


def _path(text: str) -> Path:
    return resolve_path(text)


class GoldenRefused(RuntimeError):
    """The run cannot start (a wrong data source, an output directory with answers in it,
    an unknown regression …); ``code`` is the command's exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class GoldenOptions:
    file: str
    out: str
    target: str | None = None
    only: str | None = None
    replay: str | None = None
    resume: bool = False
    no_vectors: bool = False
    no_record: bool = False
    workers: int | None = None
    force: bool = False
    as_login: str | None = None
    directory: str | None = None
    regress: str | None = None


@dataclass
class GoldenExecution:
    golden: GoldenSet
    outcomes: list[Any]
    summary: dict[str, Any]
    run: Any
    mode: str
    model: str
    build: str
    out: Path
    text: str
    live_calls: int = 0
    replayed: int = 0
    notes: list[str] = field(default_factory=list)


async def execute_golden(settings: Settings, factory, args: GoldenOptions, *,
                         llm_inner: Any = None, echo: Callable[..., None] = print,
                         created_by: str | None = None) -> GoldenExecution:
    """One golden run, recorded (unless ``no_record``) — ``make golden`` and the drift watch
    (7C). ``llm_inner``: the model client (default: the configured one, or ``replay``).
    Raises ``GoldenRefused`` when the run cannot start; never disposes the engine."""
    label = args.target or settings.app.default_target
    golden = load_golden(_path(args.file))
    if golden.drafts:
        echo(f"note: {golden.drafts} question(s) still marked `status: draft` are left out — "
             "a draft is measured once a person has corrected it")
    if golden.data_source != label and not args.force:
        raise GoldenRefused(f"{args.file} is written for {golden.data_source!r}, not {label!r} "
                            "(--force to run it anyway)", 1)
    wanted = {x.strip() for x in args.only.split(",")} if args.only else None
    if not [q for q in golden.questions if wanted is None or q.id in wanted]:
        # D99: a run of nothing would be recorded as a measurement of nothing
        raise GoldenRefused(
            f"{args.file}: no question to run — "
            + (f"all {golden.drafts} are still marked `status: draft` (delete that line in "
               "each question you have checked)" if golden.drafts and not golden.questions
               else f"--only {args.only} names none of its questions" if wanted
               else "it holds no question"), 1)
    out = _path(args.out)
    recorded = list((out / "llm").glob("*.json")) if (out / "llm").is_dir() else []
    if recorded and not (args.resume or args.replay):
        raise GoldenRefused(f"{out} holds {len(recorded)} recorded answer(s): a run into it "
                            "replays them — it reproduces that run and measures nothing new. "
                            "Give a new --out, or --resume to continue an interrupted run.", 1)
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    llm = RecordingLLMClient(llm_inner if llm_inner is not None
                             else llm_client(settings, args.replay), out / "llm")
    build = gaia.BUILD
    model_client = llm
    dropped: str | None = None
    expected: list[str] = []
    if args.regress:  # 6E part 2, P6-8: a deliberately worse run, for the fresh net's test
        from gaia.evaluation.net import (
            REGRESSION_MARK,
            PromptRegression,
            expected_breaks,
            parse_regression,
        )

        try:
            kind, what = parse_regression(args.regress)
        except ValueError as exc:
            raise GoldenRefused(str(exc), 2) from None
        build = f"{gaia.BUILD}{REGRESSION_MARK}{args.regress}"
        if kind == "prompt":
            model_client = PromptRegression(llm, args.regress)
            echo(f"DELIBERATE REGRESSION ({args.regress}): the SQL prompt goes without that "
                 "rule. This run measures whether three fresh runs catch a worse prompt (6E); "
                 "it is recorded under its own build label and says nothing about the build "
                 "itself.")
        else:
            dropped = what
            expected = expected_breaks(golden, what)
            echo(f"DELIBERATE REGRESSION ({args.regress}): every question is answered without "
                  f"{what} — in no context, unknown to the guard. Expected to break (they need "
                  f"it): {', '.join(expected) or 'none'} ({len(expected)} question(s)). This run "
                  "measures whether three fresh runs catch it (6E, P6-8); it is recorded under "
                  "its own build label and says nothing about the build itself.")
    # the caller owns the engine (the worker keeps it; the tool disposes of it)
    async with factory() as session:
        full_index = await load_retrieval_index(session, label, settings)
        policy = OPERATOR
        if args.as_login:  # answered as the chat would answer this person (5C)
            policy = await policy_for_login(session, full_index, args.as_login,
                                            args.directory)
            echo(f"as {policy.label}: {len(policy.readable)} object(s) readable"
                  + ("" if policy.pii_processor else ", personal data masked"))
        index = restrict_index(full_index, policy)
        undropped = index
        guard_catalog = full_index.catalog
        if dropped:  # P6-8: the object is gone for the context and the guard alike
            from gaia.evaluation.net import drop_object

            try:
                index = drop_object(index, dropped)
            except ValueError as exc:
                raise GoldenRefused(str(exc), 2) from None
            guard_catalog = index.catalog
        meanings = await meanings_state(session, label)  # what it answers with (D81)
        hits: dict[str, object] = {}
        if not args.no_vectors:
            from gaia.tools.retrieve import _embedder, _vectors

            embedder = _embedder(settings)
            notes: list[str] = []
            from gaia.query.ask import search_text
            from gaia.query.golden_run import AS_STATED

            try:
                for q in golden.questions:
                    if only and q.id not in only:
                        continue
                    # D91: what each call of the question searches with — alone, after
                    # its clarification, after "exactly as stated"
                    texts = [search_text(q.question)]
                    if q.clarified:
                        texts.append(search_text(q.question, [("", q.clarified)]))
                    if q.expect == "answer":
                        texts.append(search_text(q.question, [("", AS_STATED)]))
                    for text in texts:
                        if not notes:  # down once: words only
                            hits[text] = await _vectors(session, settings, index, text,
                                                        embedder, notes)
            finally:
                embedder.close()
            for n in sorted(set(notes)):
                echo(f"note: {n}")
        guard = build_access_guard(settings, label, index, guard_catalog, policy)
        reference_guard = (build_access_guard(settings, label, undropped,
                                              full_index.catalog, policy)
                           if dropped else None)
        if args.as_login:  # the truth does not depend on who asks (7B)
            reference_guard = build_access_guard(settings, label, full_index,
                                                 full_index.catalog, OPERATOR)
            held = [q.id for q in golden.questions if (not only or q.id in only)
                    and withheld_from(q, policy)]
            echo(f"withheld from {policy.label} (right only without a number): "
                  f"{', '.join(held) or 'none'}")
        calibrator = await answer_calibrator_for(session, settings)  # 6D: as the chat

        def progress(o) -> None:
            mark = {True: "ok  ", False: "NO  ", None: "--  "}[o.correct]
            echo(f"  {mark}{o.id:<4} {o.first_status:<13} -> {o.final_status:<13} "
                  f"{o.calls} call(s) {o.seconds:>6} s  {o.reason[:70]}", flush=True)

        try:
            outcomes = await asyncio.to_thread(
                run_golden, golden, index=index, guard=guard, target=settings.target(label),
                llm=model_client, settings=ask_settings(settings, label, calibrator),
                retrieval=settings.retrieval.to_settings(),
                vectors=(lambda q: hits.get(q)) if hits else None, only=only,
                workers=args.workers or settings.llm.concurrency, progress=progress,
                reference_guard=reference_guard, person=policy if args.as_login else None)
        except ValueError as exc:
            if ": the reference " not in str(exc):
                raise
            # D99: a corrected reference that no longer passes the guard or fails on the
            # database stops the run before any question is asked — one line, nothing recorded
            raise GoldenRefused(f"{exc} — fix that question in {args.file} and run again",
                                1) from None
        if not args.no_record:
            store = QueryStore(session)
            for o in outcomes:
                for a in o.answers:
                    await store.record_answer(a, data_source_id=index.data_source_id,
                                              snapshot_id=index.snapshot_id,
                                              origin="golden")
                    if a.status == "clarify" and a.clarification is not None:
                        row = await store.record_clarification(
                            a, data_source_id=index.data_source_id,
                            snapshot_id=index.snapshot_id, origin="golden")
                        q = next(x for x in golden.questions if x.id == o.id)
                        if q.clarified and len(o.answers) > 1:
                            await store.answer_clarification(row, q.clarified, None)
            await session.commit()
        summary = summarise(outcomes)
        # 7C: who the provider says answered (CallRecord.answered_by) — the drift watch
        # sees an "always the latest" name move on
        answered: dict[str, int] = {}
        for o in outcomes:
            for a in o.answers:
                for c in a.calls:
                    if c.answered_by:
                        answered[c.answered_by] = answered.get(c.answered_by, 0) + 1
        summary["answered_by"] = answered
        if args.regress:
            summary["regression"] = {"name": args.regress,
                                     "expected": expected if dropped else None}
        model = getattr(llm.inner, "model", None) or settings.llm.model
        run = None
        if not args.no_record:  # the evaluation store (6A, D80)
            run = await record_golden(
                session, data_source=label, outcomes=outcomes, summary=summary,
                question_set=args.file, out_dir=str(out), model=model, build=build,
                set_key=question_set_key((o.id, o.question, o.expect) for o in outcomes),
                live_calls=llm.live_calls, replayed_calls=llm.replayed, started_at=started,
                as_login=policy.label if args.as_login else None,
                created_by=created_by or cli_actor(), commit=git_commit(), meanings=meanings)
            await session.commit()
    mode = run_mode(llm.live_calls, llm.replayed)
    provenance = {
        "fresh": f"{llm.live_calls} model answer(s) asked now — a new measurement",
        "mixed": f"{llm.live_calls} model answer(s) asked now, {llm.replayed} replayed from "
                 "this directory (a resumed run)",
        "replay": f"REPLAYED: all {llm.replayed} model answers come from earlier recordings — "
                  "this reproduces a run, it is not a new measurement",
    }[mode]
    text = report_markdown(golden, outcomes, summary, model, build, provenance=provenance)
    (out / "report.md").write_text(text, encoding="utf-8")
    doc = {"summary": summary, "model": model, "build": build,
           "as_of": golden.as_of.isoformat(), "data_source": golden.data_source,
           "outcomes": [{k: v for k, v in o.__dict__.items() if k != "answers"}
                        for o in outcomes]}
    (out / "results.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False,
                                                 default=str), encoding="utf-8")
    echo()
    echo(text)
    echo(f"model calls: {llm.live_calls} new, {llm.replayed} replayed from {out / 'llm'}")
    if run is not None:
        echo(f"recorded as evaluation run {str(run.id)[:8]} ({run.mode}) — make quality "
             f"TARGET={label}")
    return GoldenExecution(golden=golden, outcomes=outcomes, summary=summary, run=run,
                           mode=mode, model=model, build=build, out=out, text=text,
                           live_calls=llm.live_calls, replayed=llm.replayed)
