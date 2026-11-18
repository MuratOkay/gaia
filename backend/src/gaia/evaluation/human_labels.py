"""Labels from people (Phase 6B, D83): every SME decision as a verdict on Gaia's meaning.

An SME's decision (5B) is a new, signed version that keeps the machine version it judged
(``score_breakdown.previous``). That machine version holds everything calibration needs — the
model, the prompt, the scoring version and the raw score — and the decision says how right it
was:

* **accept** → *right*;
* **correct** → *wrong* when the role or the purpose changed (Gaia misread what the column is);
  *partly* when only the name, the entity or the unit did (the reading was right, its words
  not quite); *right* when only other names, personal data or the values' meanings were added
  (the SME added knowledge, Gaia's reading stood);
* **reject** → *wrong*;
* **not used** → no label (it says nothing about the meaning).

A later decision on the same machine version replaces an earlier one; a *stale* decision (its
column changed since) still labels the version it judged. The LLM judge of 3C is never a label
(P6-0: it agrees with the owner on 86 % and errs lenient).

**The owner's 3C audit** (6B part 2, D85) is the other source: a random fifth of the judged
columns of a 3C run, each judged by the owner without the judge's verdict — yes, partial, no →
right, partly, wrong — on the answer of one condition, whose score the run kept. ``make labels
ARGS="--import-3c DIR"`` keeps them in ``evaluation.label`` (once each), with the judge's verdict
beside them: the **judge's agreement with people** (Cohen's κ) is measured there, and reported,
and read by nothing else. Only audits of a production-mode condition (names visible, full Phase 3:
``N-P``, ``N-P-raw``) count for calibration — a name-blind answer is scored from other evidence.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import ColumnRecord, DataSource, TableRecord
from gaia.db.models.evaluation import Label
from gaia.db.models.semantics import ColumnSemantic
from gaia.evaluation.metrics import auroc, cohen_kappa

__all__ = ["PRODUCTION_CONDITIONS", "HumanLabel", "all_labels", "decision_labels",
           "import_3c_audit", "judge_agreement", "label_summary", "read_3c_audit",
           "stored_labels", "verdict_of"]

# 3C conditions whose answers are scored as production scores them (names visible, Phase 3)
PRODUCTION_CONDITIONS = ("N-P", "N-P-raw")
# the 3C audit's and the judge's words
FROM_3C = {"yes": "right", "partial": "partly", "no": "wrong"}
# a label needs this many of each kind before its AUROC says anything
MIN_PER_CLASS = 10

WRONG_FIELDS = ("role", "purpose")
PARTLY_FIELDS = ("business_name", "entity", "unit")
VALUE = {"right": 1.0, "partly": 0.5, "wrong": 0.0}


@dataclass
class HumanLabel:
    data_source: str
    column: str  # schema.table.column
    verdict: str  # right | partly | wrong
    action: str
    changed: list[str]
    reviewer: str | None
    decided_at: datetime | None
    stale: bool
    blind: bool  # decided without Gaia's certainty on screen (P6-5)
    machine_version: int
    model: str
    prompt_version: str
    scoring: str  # the weights version of the score
    raw_score: float
    calibrated_score: float
    source: str = "sme"  # sme | 3c-audit
    production: bool = True  # scored as production scores (3C: N-P only)
    judge: str | None = None  # 3C: the judge's verdict on the same answer

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _changed(decided: ColumnSemantic, machine: ColumnSemantic) -> list[str]:
    out = [f for f in ("business_name", "purpose", "role", "entity", "unit", "personal")
           if (getattr(decided, f) or None) != (getattr(machine, f) or None)]
    if sorted(decided.synonyms or []) != sorted(machine.synonyms or []):
        out.append("synonyms")
    if (decided.value_domain or {}).get("meanings") != (machine.value_domain or {}).get("meanings"):
        out.append("value_meanings")
    return out


async def _machine_version(session: AsyncSession, decided: ColumnSemantic
                           ) -> ColumnSemantic | None:
    """The machine meaning a decision judged — back through earlier decisions on it (a
    second decision on the same meaning replaces the first)."""
    row = decided
    for _ in range(20):
        version = ((row.score_breakdown or {}).get("previous") or {}).get("version")
        if version is None:
            return None
        row = await session.scalar(select(ColumnSemantic).where(and_(
            ColumnSemantic.column_id == decided.column_id, ColumnSemantic.version == version)))
        if row is None or not row.reviewed_by:
            return row
    return None


def verdict_of(action: str, changed: list[str]) -> str | None:
    if action == "accept":
        return "right"
    if action == "reject":
        return "wrong"
    if action == "correct":
        if set(changed) & set(WRONG_FIELDS):
            return "wrong"
        if set(changed) & set(PARTLY_FIELDS):
            return "partly"
        return "right"
    return None  # unused


async def decision_labels(session: AsyncSession, data_source: str | None = None
                          ) -> list[HumanLabel]:
    """Every SME decision on a machine meaning, as a label, oldest first."""
    q = (select(ColumnSemantic, ColumnRecord.column_name, TableRecord.schema_name,
                TableRecord.object_name, DataSource.name)
         .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
         .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
         .join(DataSource, DataSource.id == TableRecord.data_source_id)
         .where(ColumnSemantic.reviewed_by.is_not(None)))
    if data_source:
        q = q.where(DataSource.name == data_source)
    rows = (await session.execute(q.order_by(ColumnSemantic.reviewed_at))).all()
    latest: dict[tuple[Any, int], HumanLabel] = {}
    for decided, col, schema, table, ds in rows:
        sme = (decided.score_breakdown or {}).get("sme") or {}
        action = sme.get("action")
        if not action:
            continue  # not a decision (a view column's inherited copy)
        machine = await _machine_version(session, decided)
        if machine is None:
            continue
        version = machine.version
        changed = _changed(decided, machine)
        verdict = verdict_of(action, changed)
        if verdict is None:
            continue
        latest[(decided.column_id, version)] = HumanLabel(
            data_source=ds, column=f"{schema}.{table}.{col}", verdict=verdict, action=action,
            changed=changed, reviewer=decided.reviewed_by, decided_at=decided.reviewed_at,
            stale=decided.status == "stale", blind=bool(sme.get("blind")),
            machine_version=version, model=machine.model, prompt_version=machine.prompt_version,
            scoring=machine.weights_version, raw_score=float(machine.raw_score),
            calibrated_score=float(machine.calibrated_score))
    return sorted(latest.values(), key=lambda x: (x.decided_at is None, x.decided_at))


async def stored_labels(session: AsyncSession, data_source: str | None = None
                        ) -> list[HumanLabel]:
    """The labels kept in ``evaluation.label`` (today the owner's 3C audit)."""
    q = select(Label)
    if data_source:
        q = q.where(Label.data_source == data_source)
    rows = (await session.scalars(q.order_by(Label.created_at, Label.subject))).all()
    return [HumanLabel(
        data_source=r.data_source, column=r.subject, verdict=r.verdict, action="audit",
        changed=[], reviewer=r.reviewer, decided_at=r.created_at, stale=False, blind=True,
        machine_version=0, model=r.model or "?", prompt_version=r.condition or "",
        scoring=r.scoring or "?", raw_score=float(r.raw_score or 0.0),
        calibrated_score=float(r.raw_score or 0.0), source=r.source,
        production=r.production, judge=r.judge_verdict) for r in rows]


async def all_labels(session: AsyncSession, data_source: str | None = None
                     ) -> list[HumanLabel]:
    """SME decisions and the kept labels, oldest first."""
    return [*await decision_labels(session, data_source),
            *await stored_labels(session, data_source)]


def _majority_model(calls: list[dict[str, Any]]) -> str | None:
    counts: dict[str, int] = {}
    for c in calls or []:
        if c.get("model") and not c.get("error") and c.get("purpose") != "critique":
            counts[c["model"]] = counts.get(c["model"], 0) + 1
    return max(counts, key=lambda m: (counts[m], m)) if counts else None


def read_3c_audit(out: Path, audit: Path | None = None) -> dict[str, Any]:
    """A 3C run's files: its plan, the filled audit, and — per condition audited — the
    answers' scores and the model (``results.json``) and the judge's verdicts."""
    audit = audit or out / "audit.csv"
    plan_path = out / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    with open(audit, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    conditions: dict[str, dict[str, Any]] = {}
    for code in {r.get("condition") or "" for r in rows} - {""}:
        path = out / code / "results.json"
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        jpath = out / code / "judgements.json"
        conditions[code] = {
            "rows": {f"{r['table']}.{r['column']}": r for r in doc.get("columns") or []},
            "model": _majority_model((doc.get("summary") or {}).get("calls")),
            "judged": (json.loads(jpath.read_text(encoding="utf-8")).get("verdicts") or {}
                       if jpath.exists() else {})}
    return {"plan": plan, "rows": rows, "conditions": conditions, "out": str(out.resolve())}


async def import_3c_audit(session: AsyncSession, files: dict[str, Any], *,
                          data_source: str | None = None, created_by: str | None = None,
                          reviewer: str | None = None) -> dict[str, Any]:
    """The owner's filled 3C audit (``read_3c_audit``) as labels, once each: the verdict, the
    judged answer's score, model and condition, and the judge's verdict beside it."""
    ds = files["plan"].get("data_source") or data_source
    if not ds:
        raise ValueError(f"{files['out']}: no plan.json names the data source — give TARGET")
    if data_source and data_source != ds:
        raise ValueError(f"{files['out']} is a run on {ds!r}, not {data_source!r}")
    report = {"data_source": ds, "imported": 0, "known": 0, "unfilled": 0, "missing": []}
    for row in files["rows"]:
        owner = (row.get("owner_meaning") or "").strip().lower()
        if owner not in FROM_3C:
            report["unfilled"] += 1
            continue
        code, table, column = row["condition"], row["table"], row["column"]
        cond = files["conditions"].get(code) or {"rows": {}, "judged": {}, "model": None}
        answer = cond["rows"].get(f"{table}.{column}")
        if answer is None:
            report["missing"].append(f"{code} {table}.{column}")
            continue
        origin = f"{files['out']}|{code}|{table}.{column}"
        known = await session.scalar(select(Label.id).where(Label.source == "3c-audit",
                                                            Label.origin == origin))
        if known is not None:
            report["known"] += 1
            continue
        judge = (cond["judged"].get(f"{table}.{column}") or {}).get("meaning")
        session.add(Label(
            source="3c-audit", origin=origin, data_source=ds, subject=f"{table}.{column}",
            verdict=FROM_3C[owner], judge_verdict=FROM_3C.get(judge or ""), condition=code,
            production=code in PRODUCTION_CONDITIONS, model=cond["model"],
            scoring=answer.get("weights_version") or "heuristic-v2",
            raw_score=float(answer["score"]) if answer.get("score") is not None else None,
            reviewer=reviewer, note=(row.get("owner_note") or "").strip() or None,
            created_by=created_by))
        report["imported"] += 1
    await session.flush()
    return report


def judge_agreement(labels: list[HumanLabel]) -> dict[str, Any] | None:
    """How often the LLM judge's verdict is the person's, and Cohen's κ — over the labels
    that have both (the 3C audit). Reported only: no calibrator and no gate reads it."""
    pairs = [(x.verdict, x.judge) for x in labels if x.judge]
    if not pairs:
        return None
    people, judge = [p for p, _j in pairs], [j for _p, j in pairs]
    agree = sum(1 for p, j in pairs if p == j)
    lenient = sum(1 for p, j in pairs if VALUE[j] > VALUE[p])
    return {"n": len(pairs), "agree": agree, "kappa": cohen_kappa(people, judge),
            "judge_more_lenient": lenient, "judge_stricter": len(pairs) - agree - lenient}


def label_summary(labels: list[HumanLabel]) -> dict[str, Any]:
    """Counts per verdict and per model, how well Gaia's score ranks people's verdicts
    (AUROC: right against partly or wrong), agreement in bands of the score, and — when
    some decisions were blind (P6-5) — how often people accepted with and without Gaia's
    certainty on screen."""
    def counts(xs: list[HumanLabel]) -> dict[str, int]:
        return {v: sum(1 for x in xs if x.verdict == v) for v in ("right", "partly", "wrong")}

    models: dict[str, list[HumanLabel]] = {}
    for x in labels:
        models.setdefault(x.model, []).append(x)
    bands = []
    for lo, hi in ((0.0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.01)):
        xs = [x for x in labels if lo <= x.raw_score < hi]
        bands.append({"from": lo, "to": min(hi, 1.0), "n": len(xs),
                      "right": sum(VALUE[x.verdict] for x in xs) / len(xs) if xs else None})
    decisions = [x for x in labels if x.source == "sme"]
    blind = [x for x in decisions if x.blind]
    shown = [x for x in decisions if not x.blind]
    sources: dict[str, int] = {}
    for x in labels:
        sources[x.source] = sources.get(x.source, 0) + 1
    right = sum(1 for x in labels if x.verdict == "right")
    return {
        "n": len(labels), "counts": counts(labels), "sources": sources,
        "production": sum(1 for x in labels if x.production),
        "ranking_ready": min(right, len(labels) - right) >= MIN_PER_CLASS,
        "judge": judge_agreement(labels),
        "models": {m: {"n": len(xs), **counts(xs),
                       "auroc": auroc([x.raw_score for x in xs],
                                      [x.verdict == "right" for x in xs])}
                   for m, xs in sorted(models.items())},
        "auroc": auroc([x.raw_score for x in labels], [x.verdict == "right" for x in labels]),
        "bands": bands,
        "anchoring": {"blind": len(blind), "shown": len(shown),
                      "accepted_blind": sum(1 for x in blind if x.action == "accept"),
                      "accepted_shown": sum(1 for x in shown if x.action == "accept")},
    }
