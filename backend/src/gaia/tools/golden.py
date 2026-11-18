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

``--as LOGIN`` (5C; scored since 7B, D97) answers as the chat would answer that person: a
question whose objects they may not read — or that needs personal data they may only see
masked (``personal: true``) — is *withheld*, and right only when no number comes back; every
answer is searched for the names of what they may not read. The references always run with
the operator's rights. ``make quality ARGS="--as LOGIN"`` reports such runs; they never mix
with the operator's.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.evaluation.golden_exec import GoldenOptions, GoldenRefused, execute_golden


async def _run(args: argparse.Namespace) -> int:
    """The run itself is ``gaia.evaluation.golden_exec.execute_golden`` (7C: the drift watch
    runs the same code in the worker)."""
    settings = get_settings()
    opts = GoldenOptions(**{k: getattr(args, k) for k in GoldenOptions.__dataclass_fields__})
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        await execute_golden(settings, factory, opts)
    except GoldenRefused as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    finally:
        await dispose_engine()
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
