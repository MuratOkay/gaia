"""Phase 3C: evaluate the semantic layer against ground truth.

    python -m gaia.tools.evaluate_semantics plan   --table dbo.transactions --with-neighbours
    python -m gaia.tools.evaluate_semantics labels --table dbo.transactions --with-neighbours \\
           --file verification/labels_nge_small.csv           # the owner's 40-column template
    python -m gaia.tools.evaluate_semantics run    --out evaluations/nge_small \\
           --table dbo.transactions --with-neighbours         # conditions N-C N-P B-C B-P B-P-nt
    python -m gaia.tools.evaluate_semantics judge  --out evaluations/nge_small --labels FILE
    python -m gaia.tools.evaluate_semantics audit  --out evaluations/nge_small   # owner's 20 %
    python -m gaia.tools.evaluate_semantics report --out evaluations/nge_small --labels FILE
    python -m gaia.tools.evaluate_semantics cost   --target demo_mssql          # criterion 5

``run`` reads the stored snapshot and Stage 1 evidence, runs each condition
(``gaia.evaluation.runner``) and writes everything to ``--out``; it writes
nothing to the metadata store. Profiled conditions run Stage 3 tests of their
own claims on the target (read-only; ``--no-stage3`` skips them). Every model
answer is kept in ``<out>/llm``: re-running resumes, and judging or reporting
again costs nothing for what was already asked.

Make targets: ``evaluate-plan``, ``evaluate``, ``evaluate-judge``,
``evaluate-audit``, ``evaluate-report``, ``evaluate-labels``, ``evaluate-cost``
(``ARGS=...``, ``TARGET=...``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import Settings, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.evaluation.labels import load_labels, stratified_sample, write_template
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.evaluation.report import build_report, judge_condition, write_audit
from gaia.evaluation.runner import CONDITIONS, DEFAULT_CONDITIONS, EvalContext, run_condition
from gaia.llm.base import LLMError

ROOT = Path(__file__).resolve().parents[4]


def _path(text: str) -> Path:
    """Paths are relative to the repository root (where `make` runs), not backend/."""
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


# -- loading ------------------------------------------------------------------------------


async def _load(settings: Settings, args: argparse.Namespace):
    """(label, snapshot, scope tables, stored evidence) — read only."""
    from gaia.db.repositories.profile_store import ProfileStore
    from gaia.db.repositories.semantics_store import SemanticsStore
    from gaia.introspect.service import snapshot_from_document
    from gaia.profiling.service import profiling_enabled
    from gaia.semantics.service import select_tables

    label = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            store = SemanticsStore(session)
            ds = await store.data_source(label)
            if ds is None:
                raise LookupError(f"no data source {label!r}; run `make introspect`")
            record = await store.latest_snapshot(ds.id)
            snapshot = snapshot_from_document(record.document)
            scope = select_tables(snapshot, schemas=args.schema or [], tables=args.table or [],
                                  with_neighbours=args.with_neighbours, limit=args.limit)
            evidence = await ProfileStore(session).load_evidence(
                ds.id, snapshot, enabled=profiling_enabled(settings, label))
    finally:
        await dispose_engine()
    return label, snapshot, scope, evidence


def _llm(settings: Settings, out: Path, replay: str | None):
    def inner():
        if replay:
            from gaia.llm.replay import ReplayLLMClient

            return ReplayLLMClient(_path(replay))
        if not settings.llm.api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set")
        from gaia.llm.deepseek import build_client

        return build_client(settings.llm)

    return RecordingLLMClient(inner(), out / "llm")


def _plan_file(out: Path) -> dict:
    path = out / "plan.json"
    if not path.exists():
        raise SystemExit(f"{path} not found: run `evaluate_semantics run --out {out}` first")
    return json.loads(path.read_text(encoding="utf-8"))


# -- commands -----------------------------------------------------------------------------


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    label, snapshot, scope, _ev = asyncio.run(_load(settings, args))
    codes = args.condition or DEFAULT_CONDITIONS
    columns = sum(len(t.columns) for t in scope)
    print(f"data source : {label} ({snapshot.dialect})")
    print(f"scope       : {len(scope)} object(s), {columns} column(s)")
    for t in scope:
        print(f"  {t.qualified_name} ({len(t.columns)} columns)")
    calls = 0
    for code in codes:
        c = CONDITIONS[code]
        n = 2 * len(scope) + (round(0.5 * 2 * len(scope)) if c.profiled else 0)
        calls += n + len(scope)
        print(f"  {code:<8} {c.description}: ~{n} reasoning + {len(scope)} judge call(s)")
    print(f"estimate    : ~{calls} call(s) (a second round only where Stage 3 changes evidence)")
    return 0


def cmd_labels(settings: Settings, args: argparse.Namespace) -> int:
    label, snapshot, scope, _ev = asyncio.run(_load(settings, args))
    items = stratified_sample([t for t in scope if t.kind == "table"], snapshot.dialect,
                              n=args.n, seed=args.seed)
    path = _path(args.file)
    if path.exists() and not args.overwrite:
        raise SystemExit(f"{path} exists (use --overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_template(path, label, items)
    print(f"{len(items)} column(s) to label in {path}: fill `meaning` (what the column holds, "
          "in business terms) and `role` (identifier, reference, measure, dimension, "
          "timestamp, flag, code, free-text; alternatives with |, any with *)")
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    out = _path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    label, snapshot, scope, evidence = asyncio.run(_load(settings, args))
    codes = args.condition or DEFAULT_CONDITIONS
    unknown = [c for c in codes if c not in CONDITIONS]
    if unknown:
        raise SystemExit(f"unknown condition(s): {', '.join(unknown)}; "
                         f"known: {', '.join(CONDITIONS)}")
    target = settings.target(label)
    profiler_factory = None
    if not args.no_stage3:
        from gaia.profiling.service import default_profiler

        profiler_factory = lambda: default_profiler(target, settings)  # noqa: E731
    llm = _llm(settings, out, args.replay)
    plan = {
        "data_source": label, "dialect": snapshot.dialect,
        "scope": [t.qualified_name for t in scope], "conditions": codes,
        "hide": args.hide or [], "model": settings.llm.model, "seed": args.seed,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    previous = out / "plan.json"
    if previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8"))
        plan["conditions"] = list(dict.fromkeys(old.get("conditions", []) + codes))
        if old.get("scope") != plan["scope"]:
            raise SystemExit(f"{out} holds a run over another scope; use another --out")
    previous.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    ctx = EvalContext(settings=settings, data_source=label, snapshot=snapshot,
                      scope=[t.qualified_name for t in scope], evidence=evidence, llm=llm,
                      out=out, hide=args.hide or [], profiler_factory=profiler_factory,
                      seed=args.seed)
    status = 0
    for code in codes:
        summary = run_condition(ctx, code)
        live = sum(1 for c in summary["calls"] if not c["cache"])
        print(f"{code:<8} {summary['columns']} column(s), {live} call(s), "
              f"{summary['wall_s']} s; second round: {len(summary['second_round'])}"
              + (f"; FAILED {len(summary['failed'])}" if summary["failed"] else ""))
        for note in summary["notes"] + summary["failed"]:
            print(f"           - {note}")
        status = status or (2 if summary["failed"] else 0)
    print(f"model calls: {llm.live_calls} new, {llm.replayed} replayed from {out / 'llm'}")
    return status


def cmd_judge(settings: Settings, args: argparse.Namespace) -> int:
    out = _path(args.out)
    plan = _plan_file(out)
    labels = _labels(args)
    llm = _llm(settings, out, args.replay)
    codes = args.condition or plan["conditions"]
    for code in codes:
        result = judge_condition(settings, llm, out, code, plan["data_source"], labels)
        print(f"{code:<8} judged {result['judged']} column(s), {result['calls']} call(s)"
              + (f"; errors: {result['errors']}" if result["errors"] else ""))
    return 0


def cmd_audit(settings: Settings, args: argparse.Namespace) -> int:
    out = _path(args.out)
    plan = _plan_file(out)
    path = _path(args.file) if args.file else out / "audit.csv"
    if path.exists() and not args.overwrite:
        raise SystemExit(f"{path} exists (use --overwrite)")
    n = write_audit(out, plan["conditions"], path, share=args.share, seed=args.seed)
    print(f"{n} judged column(s) to audit in {path}: fill `owner_meaning` with yes, partial "
          "or no (does the answer mean what the reference means?) — the judge's verdict "
          "is not shown, on purpose")
    return 0


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    out = _path(args.out)
    plan = _plan_file(out)
    audit = _path(args.audit) if args.audit else out / "audit.csv"
    text, doc = build_report(out, plan["data_source"], _labels(args),
                             settings.profiling.accept_at, audit)
    (out / "report.md").write_text(text, encoding="utf-8")
    (out / "report.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    print(text)
    return 0


def cmd_cost(settings: Settings, args: argparse.Namespace) -> int:
    from gaia.evaluation.cost import production_cost

    text = asyncio.run(production_cost(settings, args.target or settings.app.default_target))
    print(text)
    if args.file:
        _path(args.file).write_text(text, encoding="utf-8")
    return 0


def _labels(args: argparse.Namespace):
    labels = {}
    for path in args.labels or []:
        labels.update(load_labels(_path(path)))
    return labels


# -- main ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3C evaluation of the semantic layer.")
    sub = parser.add_subparsers(dest="command", required=True)

    def scope(p: argparse.ArgumentParser) -> None:
        p.add_argument("--target", help="data source / target name from config")
        p.add_argument("--schema", action="append")
        p.add_argument("--table", action="append")
        p.add_argument("--with-neighbours", action="store_true")
        p.add_argument("--limit", type=int)

    p = sub.add_parser("plan", help="scope, conditions and a call estimate; no model calls")
    scope(p)
    p.add_argument("--condition", action="append")
    p = sub.add_parser("labels", help="the owner's stratified labelling template (CSV)")
    scope(p)
    p.add_argument("--file", required=True)
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("run", help="run conditions (resumable)")
    scope(p)
    p.add_argument("--out", required=True)
    p.add_argument("--condition", action="append", help=f"one of {', '.join(CONDITIONS)}")
    p.add_argument("--hide", action="append", help="schema.object removed for N-P-h")
    p.add_argument("--no-stage3", action="store_true")
    p.add_argument("--replay", metavar="DIR")
    p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("judge", help="judge the answers against labels or names")
    p.add_argument("--out", required=True)
    p.add_argument("--labels", action="append", help="labels CSV (repeatable)")
    p.add_argument("--condition", action="append")
    p.add_argument("--replay", metavar="DIR")
    p = sub.add_parser("audit", help="the owner's audit sample (CSV)")
    p.add_argument("--out", required=True)
    p.add_argument("--file")
    p.add_argument("--share", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("report", help="metrics and the 3C criteria")
    p.add_argument("--out", required=True)
    p.add_argument("--labels", action="append")
    p.add_argument("--audit")
    p = sub.add_parser("cost", help="criterion 5: the production run, per table")
    p.add_argument("--target")
    p.add_argument("--file")
    args = parser.parse_args(argv)

    settings = get_settings()
    commands = {"plan": cmd_plan, "labels": cmd_labels, "run": cmd_run, "judge": cmd_judge,
                "audit": cmd_audit, "report": cmd_report, "cost": cmd_cost}
    try:
        return commands[args.command](settings, args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
