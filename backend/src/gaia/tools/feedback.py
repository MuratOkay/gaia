"""People's "Was this right?" across every data source (Phase 6D; D94, D96).

    make feedback                                   # the last 50, newest first
    make feedback ARGS="--last 10"
    make feedback ARGS="void 1a2b3c4d --note 'clicked while testing A1'"
    make feedback ARGS="unvoid 1a2b3c4d"

Each line: the id's first 8 characters, when, the data source, who said it, the verdict, the
status (``none`` for a yes, ``open`` while an SME has not decided, ``decided``), the note if any,
and who reviews that data source. A doubt on a data source **nobody reviews** waits for no one —
the line says so. This is the operator's view: the SME sees only their own data sources
(*Review → Answers people doubted*), and the person only their own answers.

``void`` sets one aside (D96): a verdict given or decided while testing is not evidence. It
stays here, marked *SET ASIDE*, and counts nowhere — no label, no count, no SME item. Audited
(``feedback_voided``); ``unvoid`` takes it back.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.db.engine import dispose_engine, get_engine
from gaia.evaluation.feedback import FeedbackError, FeedbackNotFound, all_feedback, void


async def _list(factory, last: int) -> int:
    async with factory() as s:
        rows = await all_feedback(s, limit=last)
    if not rows:
        print("no \"Was this right?\" given yet")
        return 0
    for r in reversed(rows):
        status = r["status"] if r["status"] != "decided" else (
            f"decided by {r['decided_by']}: {r['decision']}")
        if r.get("voided_at"):
            status = "SET ASIDE"
        who = ", ".join(r["reviewers"]) if r["reviewers"] else "NOBODY reviews this data source"
        note = f"“{r['note'][:60]}”" if r["note"] else "no note"
        print(f"  {r['id'][:8]}  {r['updated_at']:%Y-%m-%d %H:%M}  {r['data_source']:<12} "
              f"{r['given_by']:<10} {r['verdict']:<7} {status:<28} {note}")
        print(f"            {(r['question'] or '')[:80]}  — reviewed by: {who}")
        if r.get("voided_at"):
            print(f"            set aside {r['voided_at']:%Y-%m-%d %H:%M} by {r['voided_by']}: "
                  f"{r['void_note']}")
    return 0


async def _void(factory, ref: str, note: str | None, undo: bool) -> int:
    from gaia.auth import audit
    from gaia.auth.service import cli_actor

    actor = cli_actor()
    async with factory() as s:
        try:
            f = await void(s, ref, actor=actor, note=note, undo=undo)
        except (FeedbackError, FeedbackNotFound) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        await audit.record(s, "feedback_unvoided" if undo else "feedback_voided", actor=actor,
                           subject=f"feedback {f.id}",
                           detail={"verdict": f.verdict, "status": f.status,
                                   "decision": f.decision, "note": note})
        await s.commit()
    print(f"{'taken back' if undo else 'set aside'}: {f.id} ({f.given_by}: {f.verdict}, "
          f"{f.status}{': ' + f.decision if f.decision else ''})")
    return 0


async def _run(args: argparse.Namespace) -> int:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        if getattr(args, "command", None) in ("void", "unvoid"):
            return await _void(factory, args.id, args.note, args.command == "unvoid")
        return await _list(factory, args.last)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="People's \"Was this right?\" (6D).")
    p.add_argument("--last", type=int, default=50)
    sub = p.add_subparsers(dest="command")
    v = sub.add_parser("void", help="set one aside: no label, no count (D96)")
    v.add_argument("id", help="its id, or its first 8 characters as listed")
    v.add_argument("--note", required=True, help="why (kept and audited)")
    u = sub.add_parser("unvoid", help="take a setting-aside back")
    u.add_argument("id")
    u.add_argument("--note", default=None)
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
