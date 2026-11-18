"""Draft a golden set for a person to correct (Phase 7B, P6-6, P7-3; D97).

    make golden-draft TARGET=demo_mssql ARGS="--out infra/sqlserver/golden.yaml --as-of 2016-12-31"
    make golden-draft TARGET=demo_mssql ARGS="… --seeds 12 --per-seed 3"
    make golden-draft TARGET=demo_pg ARGS="… --replay DIR"     # recorded answers (tests)

Gaia picks small contexts (a well-connected table and its neighbours, spread over the
schemas), asks the configured model for questions with reference SQL in each, checks every
reference through the guard and runs it read-only on the target, and writes a golden file in
which **every question is ``status: draft``** — skipped by ``make golden`` until a person has
corrected it and deleted that line (``gaia.evaluation.drafting``). Each draft shows what the
reference read, how many rows it returned and the first rows (personal data as shapes).

``--as-of`` is the date relative periods are read from ("last year"): the data's own today.
Every model answer is kept in ``<out>.llm/`` (a re-run with the same prompts replays them).
Nothing is written to the metadata store.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

import gaia
from gaia.access.policy import OPERATOR, build_access_guard
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.repositories.semantics_store import SemanticsStore
from gaia.evaluation.drafting import choose_seeds, draft_questions, drafts_yaml
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.query.guard import row_limit
from gaia.query.retrieval_store import load_retrieval_index
from gaia.tools.ask import llm_client

ROOT = Path(__file__).resolve().parents[4]


def _path(text: str) -> Path:
    p = Path(text)
    return p if p.is_absolute() else ROOT / p


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    from gaia.models.registry import settings_of

    settings = await settings_of(settings, "answering")  # 7D: the model serving it now
    label = args.target or settings.app.default_target
    out = _path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists — a draft never overwrites a file a person may have corrected; "
              "give another --out (or --force)", file=sys.stderr)
        return 1
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC).date()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as s:
            unknown = await SemanticsStore(s).unknown_name(label, settings.targets)
            if unknown:
                print(unknown, file=sys.stderr)
                return 2
            index = await load_retrieval_index(s, label, settings)
    finally:
        await dispose_engine()
    reasoned = sum(1 for o in index.objects.values() if o.business_name)
    if not reasoned:
        print(f"note: no meanings for {label} yet — the drafts see names only (make reason "
              "first for better questions)")
    seeds = choose_seeds(index, args.seeds)
    print(f"{label}: {len(index.objects)} object(s), {reasoned} with a meaning; "
          f"{len(seeds)} context(s), {args.per_seed} question(s) each; as of {as_of}")
    llm = RecordingLLMClient(llm_client(settings, args.replay), Path(f"{out}.llm"))
    guard = build_access_guard(settings, label, index, index.catalog, OPERATOR)
    target = settings.target(label)

    def progress(seed: str, made, error) -> None:
        if error:
            print(f"  {seed:<32} FAILED: {error}")
            return
        marks = " ".join(d.status if d.status != "ok" else "ok" for d in made)
        print(f"  {seed:<32} {len(made)} draft(s): {marks}", flush=True)

    drafts, cost = await asyncio.to_thread(
        draft_questions, index, guard=guard, target=target, llm=llm,
        model=settings.llm.model, today=as_of, seeds=seeds, per_seed=args.per_seed,
        thinking=settings.llm.thinking.drafting, max_tokens=settings.llm.max_output_tokens,
        row_limit=row_limit(settings, label),
        timeout_s=target.limits.statement_timeout_s,
        retrieval=settings.retrieval.to_settings(), progress=progress)
    if args.no_preview:
        for d in drafts:
            d.preview = []
    text = drafts_yaml(drafts, data_source=label, as_of=as_of,
                       model=getattr(llm.inner, "model", None) or settings.llm.model,
                       build=gaia.BUILD, cost=cost)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    ok = sum(1 for d in drafts if d.status == "ok")
    print(f"\n{len(drafts)} draft(s) written to {out} — {ok} ran and returned rows. "
          f"{cost['calls']} model call(s), {cost['tokens']:,} tokens; "
          f"{cost['duplicates']} duplicate(s) dropped.")
    print("Next: correct them (the file says how), delete each `status: draft` you vouch for, "
          f"then `make golden TARGET={label} ARGS=\"--file {args.out} --out …\"`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Draft golden questions for a person to correct.")
    p.add_argument("--target")
    p.add_argument("--out", required=True, help="the golden file to write (YAML)")
    p.add_argument("--as-of", help="the data's today, YYYY-MM-DD (default: today)")
    p.add_argument("--seeds", type=int, default=10, help="contexts to draft from (default 10)")
    p.add_argument("--per-seed", type=int, default=3, help="questions per context (default 3)")
    p.add_argument("--replay", metavar="DIR", help="answers from recordings (tests)")
    p.add_argument("--no-preview", action="store_true",
                   help="leave the result previews out of the file")
    p.add_argument("--force", action="store_true", help="overwrite --out")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
