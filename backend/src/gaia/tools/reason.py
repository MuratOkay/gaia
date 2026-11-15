"""Run the semantic reasoning pass (Phase 2).

Usage:
    python -m gaia.tools.reason --dry-run                       # what would run, no LLM calls
    python -m gaia.tools.reason --table dbo.orders --with-neighbours
    python -m gaia.tools.reason --schema dbo --limit 5
    python -m gaia.tools.reason                                 # everything in scope
    python -m gaia.tools.reason --cost-report                   # per-call tokens/latency

Works from the latest stored snapshot (run ``make introspect`` first). Tables
whose structure hash, prompt versions and model are unchanged are skipped, so
a re-run over an unchanged schema makes zero LLM calls.

Phase 3 (prompt generation 2, the default): each table also gets an evidence
block — the stored profiles, tests and view lineage from ``make profile`` when
profiling is enabled for the target, else catalog only. ``--evidence-file``
replaces the store with a JSON document (format: ``gaia.profiling.summary``),
e.g. for replayed fixtures.

``--replay DIR`` answers from files instead of a provider (``gaia.llm.replay``):
requests appear in ``DIR/requests``; answers written to ``DIR/responses`` are
used on the next run. Such runs are recorded with model ``replay``, never under
a provider's name, so they cannot be mistaken for measurements of a real model.

Exit codes: 0 = all reasoned or skipped; 1 = aborted; 2 = some tables or
critiques failed (their raw payloads are in ``audit.llm_call``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.llm.base import LLMError
from gaia.llm.cache import build_cache
from gaia.llm.deepseek import build_client
from gaia.llm.embeddings import build_embedding_client
from gaia.llm.replay import ReplayLLMClient, pending_requests
from gaia.profiling.summary import JsonFileEvidence
from gaia.semantics.service import ReasoningOptions, ReasoningReport, run_reasoning


def _print_report(report: ReasoningReport, *, cost_report: bool) -> None:
    print(f"data source    : {report.data_source}")
    print(f"snapshot       : {report.snapshot_id}")
    print(f"model          : {report.model}")
    print(f"prompts        : {report.prompt_version} / {report.critique_prompt_version}")
    print(f"thinking       : semantics={report.thinking} critique={report.critique_thinking}")
    if report.prompt_major >= 2:
        print(f"generation     : 2 (evidence, heuristic-v2); values exposed as {report.exposure}")
    if report.run_id:
        print(f"run            : {report.run_id}")
    print()

    width = max((len(r.qualified_name) for r in report.results), default=10)
    for r in sorted(report.results, key=lambda x: x.qualified_name):
        line = f"  {r.qualified_name:<{width}}  {r.action:<12}"
        if r.columns:
            line += f" {r.columns:>3} cols"
        if r.mean_score is not None:
            line += f"  mean={r.mean_score:.3f}"
        if r.detail:
            line += f"  ({r.detail})"
        print(line)
        if r.rendered:
            print()
            print(r.rendered)
            print()

    print()
    print(
        f"tables         : reasoned={report.count('reasoned')} skipped={report.count('skipped')} "
        f"failed={report.count('failed')}"
        + (f" would_reason={report.count('would_reason')}" if report.dry_run else "")
    )
    if report.dry_run:
        print("dry run        : no LLM calls, nothing written")
        return

    t = report.totals()
    print(
        f"llm            : calls={t['llm_calls']} cache_hits={t['cache_hits']} "
        f"tokens in={t['prompt_tokens']} out={t['completion_tokens']} "
        f"reasoning={t['reasoning_tokens']} cached={t['cached_tokens']}"
    )
    print(
        f"wall time      : {report.wall_ms / 1000:.1f}s "
        f"(sum of call latency {t['latency_ms'] / 1000:.1f}s)"
    )
    print(
        f"embeddings     : written={report.embeddings_written} "
        f"cache_hits={report.embedding_cache_hits}"
    )
    print(f"join hints     : fk={report.fk_hints} inferred={report.inferred_hints}")
    if report.coverage:
        print(f"coverage       : {dict(sorted(report.coverage.items()))}")
    if report.prompt_major >= 2:
        print(f"review queue   : {dict(sorted(report.review.items())) or 'nothing queued'}")
    if report.stale_marked and any(report.stale_marked.values()):
        print(f"marked stale   : {report.stale_marked}")

    d = report.distribution
    if d.get("count"):
        print()
        print(
            f"score distribution over {d['count']} current columns in scope: "
            f"mean={d['mean']:.3f} median={d['median']:.3f} min={d['min']:.3f} max={d['max']:.3f}"
        )
        print(f"bands          : {d['bands']}")
        peak = max(n for _, n in report.histogram) or 1
        for label, n in report.histogram:
            bar = "#" * round(40 * n / peak)
            print(f"  {label}  {n:>4}  {bar}")

    if report.notes:
        print()
        print("notes:")
        for note in report.notes[:50]:
            print(f"  - {note}")
        if len(report.notes) > 50:
            print(f"  ... and {len(report.notes) - 50} more")

    if cost_report:
        print()
        print("calls:")
        print(f"  {'table':<{width}}  {'purpose':<9} att cache  in      out     reas    ms     ok")
        for r in sorted(report.results, key=lambda x: x.qualified_name):
            for c in r.calls:
                print(
                    f"  {r.qualified_name:<{width}}  {c.purpose:<9} {c.attempt:>3} "
                    f"{'hit' if c.cache_hit else '-':<5} {c.usage.prompt_tokens:<7} "
                    f"{c.usage.completion_tokens:<7} {c.usage.reasoning_tokens:<7} "
                    f"{c.latency_ms:<6} {'yes' if c.parsed_ok else 'NO: ' + (c.error or '')[:60]}"
                )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.replay:
        settings = settings.model_copy(
            update={"llm": settings.llm.model_copy(
                update={"provider": "replay", "replay_dir": args.replay, "model": "replay"})}
        )
    if settings.unresolved_env:
        print(f"WARN unresolved env placeholders: {', '.join(settings.unresolved_env)}")

    def llm_factory():
        if settings.llm.provider == "replay":
            if not settings.llm.replay_dir:
                raise LLMError("llm.provider is replay but llm.replay_dir is not set")
            return ReplayLLMClient(settings.llm.replay_dir)
        if not settings.llm.api_key:
            raise LLMError(
                "DEEPSEEK_API_KEY is not set; required for reasoning (not for --dry-run)"
            )
        return build_client(settings.llm)

    cache = build_cache(settings.cache.redis_url, enabled=not args.no_cache)
    embedder = None
    if not args.dry_run and not args.no_embed:
        embedder = build_embedding_client(
            settings.embeddings, cache=cache, cache_ttl_s=settings.cache.ttl_s.embedding
        )

    options = ReasoningOptions(
        target=args.target,
        schemas=args.schema or [],
        tables=args.table or [],
        with_neighbours=args.with_neighbours,
        limit=args.limit,
        thinking=False if args.no_thinking else None,
        critique_thinking=False if args.no_thinking else None,
        dry_run=args.dry_run,
        force=args.force,
        embed=False if args.no_embed else None,
        show_bundles=args.show_bundles,
    )

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            report = await run_reasoning(
                settings,
                session,
                options,
                llm_factory=llm_factory,
                embedder=embedder,
                cache=cache,
                evidence_source=JsonFileEvidence(args.evidence_file)
                if args.evidence_file
                else None,
            )
    finally:
        if embedder is not None:
            embedder.close()
        await dispose_engine()

    _print_report(report, cost_report=args.cost_report)
    if settings.llm.provider == "replay" and settings.llm.replay_dir:
        pending = pending_requests(settings.llm.replay_dir)
        if pending:
            print(f"\nreplay: {len(pending)} request(s) awaiting an answer in "
                  f"{settings.llm.replay_dir}/responses/:")
            for p in pending:
                print(f"  {p['key']}  {p['purpose']:<9} {p['table']}")
    if report.count("failed") or any(r.critique_ok is False for r in report.results):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic reasoning over the stored snapshot.")
    parser.add_argument("--target", help="data source / target name from config")
    parser.add_argument("--schema", action="append", help="limit to a schema (repeatable)")
    parser.add_argument("--table", action="append", help="schema.table or table (repeatable)")
    parser.add_argument(
        "--with-neighbours",
        action="store_true",
        help="add tables linked to the selection by a declared foreign key",
    )
    parser.add_argument("--limit", type=int, help="at most N tables (after sorting by name)")
    parser.add_argument(
        "--no-thinking", action="store_true", help="disable thinking for semantics and critique"
    )
    parser.add_argument("--dry-run", action="store_true", help="no LLM calls, no writes")
    parser.add_argument("--show-bundles", action="store_true", help="with --dry-run: print prompts")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-reason even if unchanged (cached responses are reused; add --no-cache "
        "for fresh model answers)",
    )
    parser.add_argument("--no-embed", action="store_true", help="skip the embedding step")
    parser.add_argument("--no-cache", action="store_true", help="bypass the Redis response cache")
    parser.add_argument("--cost-report", action="store_true", help="print every LLM call")
    parser.add_argument(
        "--replay", metavar="DIR", help="answer from files in DIR (model recorded as 'replay')"
    )
    parser.add_argument(
        "--evidence-file", metavar="JSON",
        help="evidence from a JSON file instead of the profile store (fixtures, experiments)",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
