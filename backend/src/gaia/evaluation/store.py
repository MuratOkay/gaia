"""Evaluation runs in the metadata store (Phase 6A, D80).

``record_golden`` keeps a golden run and its questions; ``import_golden_dir`` brings an older
run's files (``results.json``) in, as ``imported``; ``load_runs`` reads them back for the quality
report. A question set is identified by its content — the questions' ids, words and what they
expect — so runs compare exactly when they asked the same questions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.evaluation import EvaluationItem, EvaluationRun
from gaia.evaluation.quality import ItemView, RunView

__all__ = ["git_commit", "import_golden_dir", "load_runs", "meanings_state",
           "question_set_key", "record_golden", "run_mode"]

ITEM_FIELDS = ("question", "expect", "first_status", "final_status", "correct",
               "correct_first", "executable", "over_clarified", "answered_directly",
               "best_guess", "ambiguity", "repairs", "calls", "tokens", "seconds", "reason",
               "sql", "detail")


def question_set_key(questions: Iterable[tuple[str, str, str]]) -> str:
    """(id, question, expect) of every question → a short key of the set's content."""
    text = "\n".join("\t".join(q) for q in sorted(questions))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def run_mode(live: int | None, replayed: int | None) -> str:
    live, replayed = live or 0, replayed or 0
    if replayed == 0:
        return "fresh"
    return "replay" if live == 0 else "mixed"


def git_commit() -> str | None:
    from gaia.core.config import ROOT

    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h"],  # noqa: S603,S607 - fixed arguments
                             capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - no git: not recorded
        return None


async def meanings_state(session: AsyncSession, data_source: str
                         ) -> tuple[str | None, dict[str, Any]]:
    """The meanings a question is answered with, now (D81): a fingerprint of the current
    table and column semantic versions, the glossary and the confirmed joins of the data
    source, and what people decided among them."""
    from gaia.db.models.catalog import DataSource, TableRecord
    from gaia.db.models.semantics import ColumnSemantic, GlossaryTerm, JoinHint, TableSemantic

    ds = await session.scalar(select(DataSource).where(DataSource.name == data_source))
    if ds is None:
        return None, {}
    tables = (await session.execute(select(TableSemantic.id, TableSemantic.status).where(
        TableSemantic.data_source_id == ds.id, TableSemantic.is_current.is_(True)))).all()
    columns = (await session.execute(
        select(ColumnSemantic.id, ColumnSemantic.status)
        .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
        .where(TableRecord.data_source_id == ds.id, ColumnSemantic.is_current.is_(True)))).all()
    terms = (await session.execute(select(GlossaryTerm.id, GlossaryTerm.updated_at).where(
        GlossaryTerm.data_source_id == ds.id))).all()
    joins = (await session.execute(select(JoinHint.id, JoinHint.status).where(
        JoinHint.data_source_id == ds.id, JoinHint.source == "sme"))).all()
    parts = sorted(f"t{i}:{st}" for i, st in tables) + sorted(f"c{i}:{st}" for i, st in columns)
    parts += sorted(f"g{i}:{u.isoformat() if u else ''}" for i, u in terms)
    parts += sorted(f"j{i}:{st}" for i, st in joins)
    decided = sum(1 for _i, st in [*tables, *columns] if str(st).startswith("sme_"))
    facts = {"sme_decisions": decided, "glossary_terms": len(terms), "sme_joins": len(joins),
             "meanings": len(tables) + len(columns)}
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12], facts


def _item(run_id: uuid.UUID, key: str, values: dict[str, Any]) -> EvaluationItem:
    row = {f: values.get(f) for f in ITEM_FIELDS}
    for f in ("over_clarified", "answered_directly"):
        row[f] = bool(row[f])
    for f in ("repairs", "calls", "tokens"):
        row[f] = int(row[f] or 0)
    row["seconds"] = float(row["seconds"] or 0.0)
    row["reason"] = (row["reason"] or None) and str(row["reason"])[:2000]
    detail = dict(row["detail"] or {})
    for f in ("withheld", "leaked"):  # as a person (7B, D97)
        if values.get(f):
            detail[f] = list(values[f])
    row["detail"] = json.loads(json.dumps(detail, default=str)) if detail else None
    return EvaluationItem(run_id=run_id, item_key=key, **row)


async def record_golden(session: AsyncSession, *, data_source: str, outcomes: Sequence[Any],
                        summary: dict[str, Any], question_set: str | None,
                        set_key: str, out_dir: str | None, model: str | None,
                        build: str | None, live_calls: int | None, replayed_calls: int | None,
                        started_at: datetime, as_login: str | None = None,
                        created_by: str | None = None, mode: str | None = None,
                        commit: str | None = None,
                        meanings: tuple[str | None, dict[str, Any]] | None = None
                        ) -> EvaluationRun:
    run = EvaluationRun(kind="golden", data_source=data_source, question_set=question_set,
                        question_set_key=set_key, out_dir=out_dir, build=build,
                        git_commit=commit, model=model, as_login=as_login,
                        mode=mode or run_mode(live_calls, replayed_calls),
                        live_calls=live_calls, replayed_calls=replayed_calls,
                        summary=json.loads(json.dumps(summary, default=str)),
                        created_by=created_by, started_at=started_at,
                        finished_at=datetime.now(UTC),
                        meanings_key=meanings[0] if meanings else None,
                        meanings=meanings[1] if meanings else None)
    session.add(run)
    await session.flush()
    for o in outcomes:
        values = o if isinstance(o, dict) else o.__dict__
        session.add(_item(run.id, str(values["id"]), values))
    await session.flush()
    return run


async def import_golden_dir(session: AsyncSession, path: Path, *,
                            created_by: str | None = None) -> EvaluationRun | None:
    """An older golden run's ``results.json`` as an ``imported`` run; None when it is not
    one, or a run of that directory is in the store already (recorded or imported)."""
    doc_path = path / "results.json"
    if not doc_path.is_file():
        return None
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    outcomes = doc.get("outcomes") or []
    if not outcomes:
        return None
    started = datetime.fromtimestamp(doc_path.stat().st_mtime, UTC)
    known = await session.scalar(select(EvaluationRun.id).where(
        EvaluationRun.out_dir == str(path)).limit(1))
    if known is not None:
        return None
    key = question_set_key((str(o["id"]), str(o.get("question", "")), str(o.get("expect", "")))
                           for o in outcomes)
    return await record_golden(
        session, data_source=doc.get("data_source") or "?", outcomes=outcomes,
        summary=doc.get("summary") or {}, question_set=None, set_key=key, out_dir=str(path),
        model=doc.get("model"), build=doc.get("build"), live_calls=None, replayed_calls=None,
        started_at=started, created_by=created_by, mode="imported")


async def load_runs(session: AsyncSession, data_source: str, *, set_key: str | None = None,
                    ids: Sequence[str] | None = None) -> list[RunView]:
    """Golden runs of a data source (one question set), oldest first, with their items."""
    q = select(EvaluationRun).where(EvaluationRun.kind == "golden",
                                    EvaluationRun.data_source == data_source)
    if set_key:
        q = q.where(EvaluationRun.question_set_key == set_key)
    runs = list((await session.scalars(q.order_by(EvaluationRun.started_at))).all())
    if ids:
        wanted = [i.strip().lower() for i in ids if i.strip()]
        runs = [r for r in runs if any(str(r.id).startswith(w) for w in wanted)]
    out = []
    for r in runs:
        items = (await session.scalars(select(EvaluationItem).where(
            EvaluationItem.run_id == r.id))).all()
        out.append(RunView(str(r.id), r.started_at, r.build, r.model, r.mode, r.as_login, {
            i.item_key: ItemView(i.item_key, i.expect, i.correct, i.executable, i.first_status,
                                 i.final_status, i.best_guess, i.over_clarified,
                                 i.answered_directly, i.calls, i.tokens, i.seconds, i.reason,
                                 i.sql, dict(i.detail) if i.detail else None,
                                 repairs=i.repairs or 0, question=i.question)
            for i in items}, set_key=r.question_set_key, meanings_key=r.meanings_key,
            meanings=dict(r.meanings or {}),
            expected_breaks=((r.summary or {}).get("regression") or {}).get("expected"),
            answered_by=dict((r.summary or {}).get("answered_by") or {})))
    return out
