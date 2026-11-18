"""Retrieval for a business question, and its evaluation (Phase 4B).

    python -m gaia.tools.retrieve ask  --target demo_pg --question "revenue by store city"
    python -m gaia.tools.retrieve ask  --target demo_pg --question "…" --context  # + prompt
    python -m gaia.tools.retrieve eval --target demo_pg \
           --file infra/pg-target/retrieval_questions.csv
    make retrieve        TARGET=demo_pg ARGS="--question '…'"
    make retrieval-eval  TARGET=demo_pg ARGS="--file infra/pg-target/retrieval_questions.csv"
    make retrieval-eval  TARGET=demo_pg ARGS="--golden infra/pg-target/golden.yaml"   # 7B

Read-only. The question is embedded with the configured embedding model
(``embeddings.*``, Ollama ``bge-m3``) and compared with the stored meanings; when
the embedding service cannot be reached, ranking is lexical only and the output
says so (``--no-vectors`` forces that).

``eval`` reads a CSV with ``question,expected`` (``expected``: the objects a correct
answer needs, ``schema.object`` separated by ``|``; alternative sets — a view or its
base tables — separated by ``;``) and reports, per question and overall, for the
best alternative: whether every expected object is in the context (*covered*),
whether they are also *connected* by the context's joins (what the generator needs),
whether all are among the first ``--k`` ranked (*recall@k*), the reciprocal rank of
the first expected object (*MRR*), and how many objects a context holds (the price
of coverage). It is the Phase 2 "retrieval smoke test" made a
tool: "10 business questions return the expected tables in the top 5".

Clarified questions (Phase 7B, D97; D91): a ``clarified`` column holds the user's answer to a
clarification; the question is then searched as Gaia searches it after that answer — the
question and the answer together, by words and by vectors alike (``search_text``). ``--golden
FILE`` reads a golden set instead: every question with its ``objects`` as the one expected set,
and a clarify question's ``clarified`` answer — so the golden questions measure retrieval too.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import Settings, get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.query.ask import search_text
from gaia.query.retrieval import RetrievalIndex, SchemaContext, VectorHit, retrieve
from gaia.query.retrieval_store import load_retrieval_index, vector_search

ROOT = Path(__file__).resolve().parents[4]


def _path(text: str) -> Path:
    p = Path(text)
    return p if p.is_absolute() or p.exists() else ROOT / p


def _embedder(settings: Settings):
    from gaia.llm.embeddings import build_embedding_client

    return build_embedding_client(settings.embeddings)


async def _vectors(session, settings: Settings, index: RetrievalIndex, question: str,
                   embedder, notes: list[str]) -> list[VectorHit] | None:
    if embedder is None:
        return None
    try:
        vector = await asyncio.to_thread(embedder.embed_one, question)
    except Exception as exc:  # the service is down or misconfigured: lexical only
        notes.append(f"embeddings unavailable ({type(exc).__name__}: {str(exc)[:120]}): "
                     "lexical ranking only")
        return None
    return await vector_search(session, index, list(vector), settings.embeddings.model,
                               settings.retrieval.vector_k)


async def _contexts(settings: Settings, target: str, questions: list[str], use_vectors: bool
                    ) -> list[SchemaContext]:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    embedder = _embedder(settings) if use_vectors else None
    out = []
    try:
        async with factory() as session:
            index = await load_retrieval_index(session, target, settings)
            down: list[str] = []  # the embedding service failed once: words only from then
            for q in questions:
                notes: list[str] = list(down)
                hits = None
                if embedder is not None:
                    hits = await _vectors(session, settings, index, q, embedder, notes)
                    if hits is None and notes:
                        embedder.close()
                        embedder, down = None, list(notes)
                ctx = retrieve(index, q, vector_hits=hits if use_vectors else None,
                               settings=settings.retrieval.to_settings(), notes=notes)
                if not use_vectors:
                    ctx.notes = [n for n in ctx.notes if not n.startswith("vector search")]
                    ctx.notes.append("vector search off (--no-vectors)")
                out.append(ctx)
    finally:
        if embedder is not None:
            embedder.close()
        await dispose_engine()
    return out


def _print(ctx: SchemaContext, show_context: bool) -> None:
    print(f"question       : {ctx.question}")
    print(f"data source    : {ctx.data_source} ({ctx.dialect}), snapshot {ctx.snapshot_id}")
    print("ranking        :")
    chosen = {r.obj.key for r in ctx.objects}
    for key, rel in ctx.ranking[:10]:
        print(f"  {'*' if key in chosen else ' '} {rel:7.4f}  {key}")
    print("context        :")
    for r in ctx.objects:
        tag = "bridge" if r.bridge else f"#{r.rank}"
        print(f"  {tag:<7} {r.obj.key:<36} {len(r.columns)}/{len(r.obj.columns)} columns; "
              + "; ".join(str(m) for m in r.matches[:4]))
    for e in ctx.joins:
        print(f"  join    {e.from_table}.{e.from_column} = {e.to_table}.{e.to_column} ({e.kind})")
    if ctx.uncertain:
        print(f"uncertain      : {len(ctx.uncertain)} column meaning(s) in the context")
    for n in ctx.notes:
        print(f"note           : {n}")
    print(f"size           : ~{ctx.estimate_tokens():,} tokens")
    if show_context:
        print("\n" + ctx.render())


def cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    target = args.target or settings.app.default_target
    (ctx,) = asyncio.run(_contexts(settings, target, [args.question], not args.no_vectors))
    if args.json:
        print(json.dumps({
            "question": ctx.question, "tables": ctx.tables,
            "ranking": ctx.ranking[:20], "joins": [e.__dict__ for e in ctx.joins],
            "uncertain": ctx.uncertain, "notes": ctx.notes, "context": ctx.render(),
        }, indent=1, ensure_ascii=False))
    else:
        _print(ctx, args.context)
    return 0


def _load_questions(path: Path) -> list[tuple[str, list[list[str]], str | None]]:
    """(question, expected alternatives, the clarification's answer or None)."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if (r.get("question") or "").strip()
                and not r["question"].lstrip().startswith("#")]
    return [(r["question"].strip(),
             [[e.strip() for e in alt.split("|") if e.strip()]
              for alt in r["expected"].split(";") if alt.strip()],
             (r.get("clarified") or "").strip() or None)
            for r in rows]


def _golden_questions(path: Path) -> list[tuple[str, list[list[str]], str | None]]:
    """A golden set's questions with their objects (7B): those without objects — a question
    the data cannot answer — say nothing about retrieval and are left out."""
    from gaia.query.golden import load_golden

    return [(q.question, [list(q.objects)], q.clarified)
            for q in load_golden(path).questions if q.objects]


def searched(question: str, clarified: str | None) -> str:
    """What retrieval reads for a question — with its clarification's answer (D91)."""
    return search_text(question, [("", clarified)]) if clarified else question


def _connected(ctx: SchemaContext, objects: list[str]) -> bool:
    """All ``objects`` in one component of the context's joins."""
    if len(objects) <= 1:
        return all(o.lower() in {t.lower() for t in ctx.tables} for o in objects)
    parent = {o.lower(): o.lower() for o in ctx.tables}

    def root(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for e in ctx.joins:
        parent[root(e.from_table.lower())] = root(e.to_table.lower())
    return len({root(o.lower()) for o in objects}) == 1 and all(
        o.lower() in parent for o in objects)


def evaluate(contexts: list[SchemaContext], expected: list[list[list[str]]], k: int) -> dict:
    per = []
    for ctx, alternatives in zip(contexts, expected, strict=True):
        ranked = [key.lower() for key, _v in ctx.ranking]
        in_ctx = {t.lower() for t in ctx.tables}
        scored = []
        for exp in alternatives:
            ranks = {e: (ranked.index(e.lower()) + 1 if e.lower() in ranked else None)
                     for e in exp}
            first = min((r for r in ranks.values() if r), default=None)
            scored.append({
                "expected": exp, "ranks": ranks,
                "covered": all(e.lower() in in_ctx for e in exp),
                "connected": all(e.lower() in in_ctx for e in exp) and _connected(ctx, exp),
                "recall_at_k": all(r is not None and r <= k for r in ranks.values()),
                "rr": 1.0 / first if first else 0.0,
            })
        best = max(scored, key=lambda x: (x["connected"], x["covered"], x["recall_at_k"],
                                          x["rr"]))
        per.append(best | {"question": ctx.question, "alternatives": len(alternatives),
                           "context": ctx.tables, "size": len(ctx.tables),
                           "tokens": ctx.estimate_tokens(),
                           "notes": ctx.notes})
    n = len(per) or 1
    return {
        "k": k, "questions": len(per),
        "covered": sum(p["covered"] for p in per) / n,
        "connected": sum(p["connected"] for p in per) / n,
        "mean_objects": sum(p["size"] for p in per) / n,
        "recall_at_k": sum(p["recall_at_k"] for p in per) / n,
        "mrr": sum(p["rr"] for p in per) / n,
        "mean_tokens": sum(p["tokens"] for p in per) / n,
        "per_question": per,
    }


def cmd_eval(settings: Settings, args: argparse.Namespace) -> int:
    target = args.target or settings.app.default_target
    if bool(args.file) == bool(args.golden):
        print("give --file (a CSV) or --golden (a golden set)", file=sys.stderr)
        return 2
    items = (_golden_questions(_path(args.golden)) if args.golden
             else _load_questions(_path(args.file)))
    contexts = asyncio.run(_contexts(settings, target, [searched(q, c) for q, _e, c in items],
                                     not args.no_vectors))
    report = evaluate(contexts, [e for _q, e, _c in items], args.k)
    for p, (q, _e, c) in zip(report["per_question"], items, strict=True):
        p["question"], p["clarified"] = q, c
    clarified = sum(1 for _q, _e, c in items if c)
    lines = [f"retrieval evaluation: {target}, {report['questions']} question(s)"
             + (f" ({clarified} searched with their clarification)" if clarified else "")
             + f", vectors {'off' if args.no_vectors else 'on'}"]
    for p in report["per_question"]:
        marks = " ".join(f"{e}@{r if r else '-'}" for e, r in p["ranks"].items())
        flag = "ok  " if p["connected"] else ("part" if p["covered"] else "MISS")
        lines.append(f"  {flag} {('(c) ' if p['clarified'] else '') + p['question']:<60.60} "
                     f"{marks}")
        if not p["covered"]:
            lines.append(f"       context: {', '.join(p['context'])}")
    vec_notes = {n for p in report["per_question"] for n in p["notes"] if "embedding" in n}
    lines += [f"  note: {n}" for n in sorted(vec_notes)]
    lines.append(f"covered {report['covered']:.0%} · connected {report['connected']:.0%} · "
                 f"recall@{args.k} "
                 f"{report['recall_at_k']:.0%} · MRR {report['mrr']:.2f} · "
                 f"{report['mean_objects']:.1f} objects, ~{report['mean_tokens']:.0f} tokens "
                 "per context")
    text = "\n".join(lines)
    print(text)
    if args.out:
        out = _path(args.out)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                       encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval for business questions.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ask", help="the context for one question")
    p.add_argument("--target")
    p.add_argument("--question", required=True)
    p.add_argument("--no-vectors", action="store_true")
    p.add_argument("--context", action="store_true", help="print the rendered context")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("eval", help="recall over a question set")
    p.add_argument("--target")
    p.add_argument("--file", help="a CSV: question, expected[, notes, clarified]")
    p.add_argument("--golden", help="a golden set: its questions and objects (7B)")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--no-vectors", action="store_true")
    p.add_argument("--out", help="write the full report (JSON)")
    args = parser.parse_args(argv)
    settings = get_settings()
    return {"ask": cmd_ask, "eval": cmd_eval}[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
