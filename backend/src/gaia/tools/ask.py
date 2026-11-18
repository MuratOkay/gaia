"""Ask a business question (Phase 4C).

    make ask TARGET=demo_pg ARGS="--question 'Which store city sold the most last month?'"
    make ask TARGET=demo_pg ARGS="--question '…' --clarified 'order amount, cancelled excluded'"
    make ask TARGET=demo_pg ARGS="--question '…' --show-sql --as-of 2026-01-15"

Retrieval (4B) → the model writes one query or asks back → the SQL guard (4A) →
read-only execution → up to ``query.max_repair_attempts`` repairs → the narrative.
Prints the narrative and the result table (P4-2); the SQL only with ``--show-sql``.
A clarification is printed with the command to answer it (P4-4: clarify always)
and stored (D61) with Gaia's best guess; answer it with ``--clarification ID
--clarified N`` (an option's number, or your own words). Choosing the option Gaia
would have chosen answers from its stored best guess, without a new generation
call. Which option that is, is not shown: it would pull answers towards it and
spoil the measurement of how often Gaia's choice is the user's.
Every attempt is recorded in ``chat.query_run`` and every model call in
``audit.llm_call`` (``--no-record`` to skip).

``--replay DIR`` answers from files instead of the configured model (functional
tests, D30; recorded as model ``replay``). Exit codes: 0 answered, 4 clarification
needed, 5 cannot be answered, 3 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.access.policy import OPERATOR, build_access_guard, policy_for_login, restrict_index
from gaia.core.config import Settings, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.query_store import QueryStore, match_option
from gaia.llm.base import LLMError
from gaia.query.ask import Answer, AskSettings, answer_dict, ask
from gaia.query.guard import row_limit
from gaia.query.planner import SqlPlan
from gaia.query.retrieval_store import load_retrieval_index

ROOT = Path(__file__).resolve().parents[4]
EXIT = {"answered": 0, "clarify": 4, "cannot_answer": 5, "failed": 3, "refused": 6}


def ask_settings(settings: Settings, target: str, answer_calibrator=None) -> AskSettings:
    """``answer_calibrator``: the one in use for the model (6D, ``answer_calibrator_for``)."""
    cfg = settings.llm
    return AskSettings(
        model=cfg.model, row_limit=row_limit(settings, target),
        max_repair_attempts=settings.query.max_repair_attempts,
        rows_to_model=settings.query.rows_to_model,
        thinking_generation=cfg.thinking.sql_generation, thinking_repair=cfg.thinking.repair,
        thinking_answer=cfg.thinking.answer, max_tokens=cfg.max_output_tokens,
        temperature=cfg.temperature,
        raw_values_to_model=cfg.locality == "local" or settings.profiling.allow_raw_to_remote,
        statement_timeout_s=settings.target(target).limits.statement_timeout_s,
        answer_calibrator=answer_calibrator, answer_high=settings.calibration.answer_high,
        answer_medium=settings.calibration.answer_medium)


async def answer_calibrator_for(session, settings: Settings):
    """The answer calibrator in use for the configured model (6D), or None."""
    from gaia.evaluation.calibrators import ANSWER_SCORING, in_use

    return (await in_use(session, "answer")).get((settings.llm.model, ANSWER_SCORING))


def llm_client(settings: Settings, replay: str | None):
    """The configured model; a replay directory instead with ``--replay DIR`` or
    ``llm.provider: replay`` + ``llm.replay_dir`` (D30)."""
    if not replay and settings.llm.provider == "replay":
        replay = settings.llm.replay_dir
        if not replay:
            raise LLMError("llm.provider is replay but llm.replay_dir is not set")
    if replay:
        from gaia.llm.replay import ReplayLLMClient

        path = Path(replay)
        return ReplayLLMClient(path if path.is_absolute() or path.exists() else ROOT / path)
    if not settings.llm.api_key:
        raise LLMError("DEEPSEEK_API_KEY is not set")
    from gaia.llm.deepseek import build_client

    return build_client(settings.llm)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    label = args.target or settings.app.default_target
    today = date.fromisoformat(args.as_of) if args.as_of else (
        settings.query.as_of or date.today())
    llm = llm_client(settings, args.replay)
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            full_index = await load_retrieval_index(session, label, settings)
            policy = OPERATOR
            if args.as_login:  # answered as the chat would answer this person (5C)
                policy = await policy_for_login(session, full_index, args.as_login,
                                                args.directory)
            index = restrict_index(full_index, policy)
            notes: list[str] = []
            hits = None
            if not args.no_vectors:
                from gaia.tools.retrieve import _embedder, _vectors

                embedder = _embedder(settings)
                try:
                    hits = await _vectors(session, settings, index, args.question, embedder,
                                          notes)
                finally:
                    embedder.close()
            guard = build_access_guard(settings, label, index, full_index.catalog, policy)
            store = QueryStore(session)
            clarifications = [("", c) for c in args.clarified or []]
            stored, preset = None, None
            if args.clarification:
                stored = await store.clarification(uuid.UUID(args.clarification))
                if stored is None:
                    print(f"no clarification {args.clarification}", file=sys.stderr)
                    return 1
                if not args.clarified:
                    print("give your answer with --clarified", file=sys.stderr)
                    return 1
            elif args.clarified and args.question:
                # No id: the answer is to the clarification this question last got, if one
                # is open — so its question travels with the answer (5.0, T6).
                stored = await store.latest_open_clarification(index.data_source_id,
                                                               args.question)
                if stored is not None:
                    notes.append(f"answers the open clarification {stored.id}")
            if stored is not None:
                args.question = stored.question
                said = args.clarified[0]
                chosen = match_option(said, stored.options)
                text = stored.options[chosen] if chosen is not None else said
                clarifications = [(stored.clarification_question, text)]
                if (chosen is not None and chosen == stored.default_option
                        and stored.best_guess_sql and stored.best_guess_valid):
                    preset = SqlPlan(decision="answer",
                                     interpretation=stored.best_guess_interpretation or "",
                                     assumptions=list(stored.best_guess_assumptions or []),
                                     sql=stored.best_guess_sql)
            calibrator = await answer_calibrator_for(session, settings)
            answer = await asyncio.to_thread(
                ask, args.question, index=index, guard=guard, target=settings.target(label),
                llm=llm, settings=ask_settings(settings, label, calibrator), today=today,
                clarifications=clarifications, vector_hits=hits,
                retrieval=settings.retrieval.to_settings(), notes=notes, preset=preset)
            if not args.no_record:
                runs = await store.record_answer(
                    answer, data_source_id=index.data_source_id,
                    snapshot_id=index.snapshot_id, origin="cli")
                if answer.status == "clarify" and answer.clarification is not None:
                    row = await store.record_clarification(
                        answer, data_source_id=index.data_source_id,
                        snapshot_id=index.snapshot_id, origin="cli")
                    answer.notes.append(f"clarification id: {row.id}")
                    args.clarification_id = str(row.id)
                if stored is not None:
                    await store.answer_clarification(stored, args.clarified[0],
                                                     runs[0].id if runs else None)
                await session.commit()
    finally:
        await dispose_engine()
        close = getattr(llm, "close", None)
        if close:
            close()
    if args.json:
        print(json.dumps(answer_dict(answer, with_sql=args.show_sql), indent=1,
                         ensure_ascii=False))
    else:
        _print(answer, args)
    return EXIT[answer.status]


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v:,.2f}" if v != v.to_integral_value() else f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    text = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return text if len(text) <= 40 else text[:37] + "..."


def _table(a: Answer, limit: int) -> list[str]:
    rows = [[_fmt(v) for v in r] for r in a.rows[:limit]]
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(a.columns)]
    numeric = [all(isinstance(r[i], (int, float, Decimal)) or r[i] is None for r in a.rows)
               for i in range(len(a.columns))]
    out = ["  " + " | ".join(c.ljust(w) for c, w in zip(a.columns, widths, strict=True)),
           "  " + "-+-".join("-" * w for w in widths)]
    for r in rows:
        out.append("  " + " | ".join(v.rjust(w) if numeric[i] else v.ljust(w)
                                     for i, (v, w) in enumerate(zip(r, widths, strict=True))))
    if len(a.rows) > limit:
        out.append(f"  … {len(a.rows) - limit} more row(s) (--rows N)")
    if a.row_cap_reached:
        out.append("  (stopped at the row limit; there may be more)")
    return out


def _print(a: Answer, args: argparse.Namespace) -> None:
    if a.status == "answered":
        print(a.narrative or "")
        print()
        print("\n".join(_table(a, args.rows)))
        print()
        if a.confidence:
            print(f"confidence     : {a.confidence.level} ({a.confidence.score:.2f})")
            for r in a.confidence.reasons:
                print(f"                 - {r}")
        print(f"computed       : {a.interpretation}")
        for x in a.assumptions:
            print(f"assumption     : {x}")
        for x in a.uncertain:
            print(f"unconfirmed    : {x}")
    elif a.status == "clarify":
        c = a.clarification
        print(f"clarification  : {c.question if c else '?'}")
        for i, o in enumerate(c.options if c else [], start=1):
            print(f"                 {i}. {o}")
        cid = getattr(args, "clarification_id", None)
        if cid:
            print(f"\nanswer with    : make ask ARGS=\"--clarification {cid} "
                  "--clarified <number or your own words>\"")
        else:
            print(f"\nanswer with    : make ask ARGS=\"--question '{args.question}' "
                  "--clarified '<your answer>'\"")
    elif a.status == "cannot_answer":
        print(f"cannot answer  : {a.reason}")
    elif a.status == "refused":
        print(f"refused        : {a.reason} ({a.refusal})")
    else:
        print(f"failed         : {a.reason}")
    if args.show_sql:
        for t in a.attempts:
            mark = "ok" if t.ok else "failed"
            print(f"\nattempt {t.number} ({mark}):")
            for line in (t.guard.sql or t.sql).splitlines():
                print(f"  {line}")
            if t.problem:
                print(f"  problem: {t.problem.splitlines()[0][:300]}")
    for n in a.notes:
        print(f"note           : {n}")
    tokens = sum(c.usage.prompt_tokens + c.usage.completion_tokens for c in a.calls)
    print(f"model          : {len(a.calls)} call(s), {tokens:,} tokens; "
          f"{a.elapsed_ms / 1000:.1f} s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask a business question.")
    parser.add_argument("--target")
    parser.add_argument("--question", help="the question (not needed with --clarification)")
    parser.add_argument("--clarified", action="append",
                        help="your answer to Gaia's clarifying question (repeatable)")
    parser.add_argument("--clarification", metavar="ID",
                        help="the stored clarification --clarified answers (D61)")
    parser.add_argument("--as-of", help="today's date for relative periods (YYYY-MM-DD)")
    parser.add_argument("--show-sql", action="store_true")
    parser.add_argument("--rows", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-vectors", action="store_true")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--replay", metavar="DIR")
    parser.add_argument("--as", dest="as_login", metavar="LOGIN",
                        help="answer as this person would be answered in the chat (5C)")
    parser.add_argument("--directory", help="with --as: the person's directory")
    args = parser.parse_args(argv)
    if not args.question and not args.clarification:
        parser.error("give --question, or --clarification ID with --clarified")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
