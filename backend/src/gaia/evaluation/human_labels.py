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
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import ColumnRecord, DataSource, TableRecord
from gaia.db.models.semantics import ColumnSemantic
from gaia.evaluation.metrics import auroc

__all__ = ["HumanLabel", "decision_labels", "label_summary", "verdict_of"]

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
    blind = [x for x in labels if x.blind]
    shown = [x for x in labels if not x.blind]
    return {
        "n": len(labels), "counts": counts(labels),
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
