"""Run a golden set (Phase 4D).

    make golden TARGET=demo_pg \
         ARGS="--file infra/pg-target/golden.yaml --out evaluations/golden_demo_pg"
    make golden TARGET=demo_pg ARGS="… --only g01,o03"        # some questions
    make golden TARGET=demo_pg ARGS="… --replay DIR"          # answers from files (tests)

Every question goes through the whole pipeline (``gaia.query.golden_run``); the
references run through the same guard and executor. Writes ``<out>/results.json``
and ``<out>/report.md`` and prints the report. Every model answer is kept in
``<out>/llm``: an interrupted run resumes, a repeated run costs nothing already
asked. Runs and clarifications are recorded with ``origin = golden`` (kept out of
the clarification agreement users produce, D61); ``--no-record`` skips that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

import gaia
from gaia.access.policy import OPERATOR, build_access_guard, policy_for_login, restrict_index
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.query_store import QueryStore
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.query.golden import load_golden
from gaia.query.golden_run import report_markdown, run_golden, summarise
from gaia.query.retrieval_store import load_retrieval_index
from gaia.tools.ask import ask_settings, llm_client

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
    out.mkdir(parents=True, exist_ok=True)
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    llm = RecordingLLMClient(llm_client(settings, args.replay), out / "llm")
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
            hits: dict[str, object] = {}
            if not args.no_vectors:
                from gaia.tools.retrieve import _embedder, _vectors

                embedder = _embedder(settings)
                notes: list[str] = []
                try:
                    for q in golden.questions:
                        if (not only or q.id in only) and not notes:  # down once: words only
                            hits[q.question] = await _vectors(session, settings, index,
                                                              q.question, embedder, notes)
                finally:
                    embedder.close()
                for n in sorted(set(notes)):
                    print(f"note: {n}")
            guard = build_access_guard(settings, label, index, full_index.catalog, policy)

            def progress(o) -> None:
                mark = {True: "ok  ", False: "NO  ", None: "--  "}[o.correct]
                print(f"  {mark}{o.id:<4} {o.first_status:<13} -> {o.final_status:<13} "
                      f"{o.calls} call(s) {o.seconds:>6} s  {o.reason[:70]}", flush=True)

            outcomes = await asyncio.to_thread(
                run_golden, golden, index=index, guard=guard, target=settings.target(label),
                llm=llm, settings=ask_settings(settings, label),
                retrieval=settings.retrieval.to_settings(),
                vectors=(lambda q: hits.get(q)) if hits else None, only=only,
                workers=args.workers or settings.llm.concurrency, progress=progress)
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
    finally:
        await dispose_engine()
    summary = summarise(outcomes)
    model = getattr(llm.inner, "model", None) or settings.llm.model
    text = report_markdown(golden, outcomes, summary, model, gaia.BUILD)
    (out / "report.md").write_text(text, encoding="utf-8")
    doc = {"summary": summary, "model": model, "build": gaia.BUILD,
           "as_of": golden.as_of.isoformat(), "data_source": golden.data_source,
           "outcomes": [{k: v for k, v in o.__dict__.items() if k != "answers"}
                        for o in outcomes]}
    (out / "results.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False,
                                                 default=str), encoding="utf-8")
    print()
    print(text)
    print(f"model calls: {llm.live_calls} new, {llm.replayed} replayed from {out / 'llm'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a golden question set.")
    p.add_argument("--target")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--only", help="question ids, comma separated")
    p.add_argument("--replay", metavar="DIR")
    p.add_argument("--no-vectors", action="store_true")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--workers", type=int, help="questions at a time (default llm.concurrency)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--as", dest="as_login", metavar="LOGIN",
                   help="answer as this person would be answered in the chat (need-to-know)")
    p.add_argument("--directory", help="with --as: the person's directory")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
