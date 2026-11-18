"""Run a golden set (Phase 4D).

    make golden TARGET=demo_pg \
         ARGS="--file infra/pg-target/golden.yaml --out evaluations/golden_demo_pg"
    make golden TARGET=demo_pg ARGS="… --only g01,o03"        # some questions
    make golden TARGET=demo_pg ARGS="… --replay DIR"          # answers from files (tests)
    make golden TARGET=demo_pg ARGS="… --resume"              # continue an interrupted run
    make golden TARGET=demo_pg ARGS="… --regress joins-rule"  # 6E: a deliberately worse prompt
    make golden TARGET=demo_pg ARGS="… --regress drop:store.stores"  # P6-8: without one object

Every question goes through the whole pipeline (``gaia.query.golden_run``); the
references run through the same guard and executor. Writes ``<out>/results.json``
and ``<out>/report.md`` and prints the report. Every model answer is kept in
``<out>/llm``. Since D80 a directory that holds recorded answers is refused unless
``--resume`` says the run continues an interrupted one: a run into it would replay them,
which reproduces a measurement and is not a new one (OWNER_VERIFICATION §26c F6). Each run
is recorded in the evaluation store (fresh, replayed or mixed) for ``make quality``; the
report says at its top how many answers were asked now. Runs and clarifications are also
recorded with ``origin = golden`` (kept out of the clarification agreement users produce,
D61); ``--no-record`` skips all recording.

``--regress NAME`` (6E part 2, D87) sends the SQL prompt without one rule and records the run
under the build label ``<build> + deliberate regression: NAME`` — three such runs against three
of the build itself (``make quality ARGS="--compare-builds"``) are the fresh net's acceptance
test: does it catch a worse prompt? ``--regress drop:<schema.object>`` (P6-8, D90) answers
without one object instead — the questions that need it (the golden set's ``objects``) are
known before the run, so the comparison can say how many of them it named as broken.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

import gaia
from gaia.access.policy import OPERATOR, build_access_guard, policy_for_login, restrict_index
from gaia.auth.service import cli_actor
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.query_store import QueryStore
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.evaluation.store import (
    git_commit,
    meanings_state,
    question_set_key,
    record_golden,
    run_mode,
)
from gaia.query.golden import load_golden
from gaia.query.golden_run import report_markdown, run_golden, summarise
from gaia.query.retrieval_store import load_retrieval_index
from gaia.tools.ask import answer_calibrator_for, ask_settings, llm_client

ROOT = Path(__file__).resolve().parents[4]


def _path(text: str) -> Path:
    p = Path(text)
    return p if p.is_absolute() or p.exists() else ROOT / p


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    golden = load_golden(_path(args.file))
    if golden.data_source != label and not args.force:
        print(f"{args.file} is written for {golden.data_source!r}, not {label!r} "
              "(--force to run it anyway)", file=sys.stderr)
        return 1
    out = _path(args.out)
    recorded = list((out / "llm").glob("*.json")) if (out / "llm").is_dir() else []
    if recorded and not (args.resume or args.replay):
        print(f"{out} holds {len(recorded)} recorded answer(s): a run into it replays them — "
              "it reproduces that run and measures nothing new. Give a new --out, or --resume "
              "to continue an interrupted run.", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    llm = RecordingLLMClient(llm_client(settings, args.replay), out / "llm")
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
            print(str(exc), file=sys.stderr)
            return 2
        build = f"{gaia.BUILD}{REGRESSION_MARK}{args.regress}"
        if kind == "prompt":
            model_client = PromptRegression(llm, args.regress)
            print(f"DELIBERATE REGRESSION ({args.regress}): the SQL prompt goes without that "
                  "rule. This run measures whether three fresh runs catch a worse prompt (6E); "
                  "it is recorded under its own build label and says nothing about the build "
                  "itself.")
        else:
            dropped = what
            expected = expected_breaks(golden, what)
            print(f"DELIBERATE REGRESSION ({args.regress}): every question is answered without "
                  f"{what} — in no context, unknown to the guard. Expected to break (they need "
                  f"it): {', '.join(expected) or 'none'} ({len(expected)} question(s)). This run "
                  "measures whether three fresh runs catch it (6E, P6-8); it is recorded under "
                  "its own build label and says nothing about the build itself.")
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            full_index = await load_retrieval_index(session, label, settings)
            policy = OPERATOR
            if args.as_login:  # answered as the chat would answer this person (5C)
                policy = await policy_for_login(session, full_index, args.as_login,
                                                args.directory)
                print(f"as {policy.label}: {len(policy.readable)} object(s) readable"
                      + ("" if policy.pii_processor else ", personal data masked"))
            index = restrict_index(full_index, policy)
            undropped = index
            guard_catalog = full_index.catalog
            if dropped:  # P6-8: the object is gone for the context and the guard alike
                from gaia.evaluation.net import drop_object

                try:
                    index = drop_object(index, dropped)
                except ValueError as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
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
                    print(f"note: {n}")
            guard = build_access_guard(settings, label, index, guard_catalog, policy)
            reference_guard = (build_access_guard(settings, label, undropped,
                                                  full_index.catalog, policy)
                               if dropped else None)
            calibrator = await answer_calibrator_for(session, settings)  # 6D: as the chat

            def progress(o) -> None:
                mark = {True: "ok  ", False: "NO  ", None: "--  "}[o.correct]
                print(f"  {mark}{o.id:<4} {o.first_status:<13} -> {o.final_status:<13} "
                      f"{o.calls} call(s) {o.seconds:>6} s  {o.reason[:70]}", flush=True)

            outcomes = await asyncio.to_thread(
                run_golden, golden, index=index, guard=guard, target=settings.target(label),
                llm=model_client, settings=ask_settings(settings, label, calibrator),
                retrieval=settings.retrieval.to_settings(),
                vectors=(lambda q: hits.get(q)) if hits else None, only=only,
                workers=args.workers or settings.llm.concurrency, progress=progress,
                reference_guard=reference_guard)
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
                    created_by=cli_actor(), commit=git_commit(), meanings=meanings)
                await session.commit()
    finally:
        await dispose_engine()
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
    print()
    print(text)
    print(f"model calls: {llm.live_calls} new, {llm.replayed} replayed from {out / 'llm'}")
    if run is not None:
        print(f"recorded as evaluation run {str(run.id)[:8]} ({run.mode}) — make quality "
              f"TARGET={label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a golden question set.")
    p.add_argument("--target")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--only", help="question ids, comma separated")
    p.add_argument("--replay", metavar="DIR")
    p.add_argument("--resume", action="store_true",
                   help="continue an interrupted run in --out (its answers are replayed)")
    p.add_argument("--no-vectors", action="store_true")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--workers", type=int, help="questions at a time (default llm.concurrency)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--as", dest="as_login", metavar="LOGIN",
                   help="answer as this person would be answered in the chat (need-to-know)")
    p.add_argument("--directory", help="with --as: the person's directory")
    p.add_argument("--regress", metavar="joins-rule|drop:SCHEMA.OBJECT",
                   help="6E: send the SQL prompt without this rule (a deliberate regression, "
                        "recorded under its own build label)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
