"""SME review (Phase 5B; D16, D69, D73): the queue, everything Gaia knows about an item,
and the SME's decisions.

**A decision is a new version** of the column's (or the table's) meaning, signed
(``reviewed_by``, ``reviewed_at``, ``review_note``) and with status ``sme_reviewed`` or
``sme_rejected``:

* ``accept`` — Gaia's meaning is right, as it stands;
* ``correct`` — right once edited: name, purpose, role, entity, unit, synonyms, and what
  each value of a code means (``value_meanings``: ``{"2": "overdue"}``);
* ``reject`` — wrong, and the SME says why (a note is required); the meaning is no longer
  used anywhere (retrieval skips ``sme_rejected``, R2);
* ``unused`` — the column is not used (legacy, empty): it leaves retrieval.

An SME-reviewed meaning is **certain**: score 1.0, band ``accepted``; a rejection 0.0,
``abstain``. The machine's last version stays in the history — Phase 6 calibrates the
scores against exactly these pairs. The decision enters the ledger as ``sme`` evidence,
closes the column's review item, and is **kept by re-reasoning** while the column's
structure is unchanged (``SemanticsStore.write_table_version``); when the column changes it
goes ``stale``, still signed, and shows in the "stale decisions" list.

**Where lineage is certain, the decision travels**: pass-through view columns that show
this column inherit it at once (a re-score of those views, no model call). **Where it is
not**, it is not copied: computed view columns that use this column are queued for a look
(``input_reviewed``).

Value meanings and glossary terms reach the SQL generator (``query.retrieval``): a code
column's values are shown with what they mean, and a glossary term that occurs in a question
comes with its definition.

Personal data (D69): a profile's values of a personal-data column are shown to an SME who
is not a PII processor as shapes only (``aaaa.aaaa@aaaa.aaa``).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings
from gaia.db.models.catalog import ColumnRecord, DataSource, TableRecord
from gaia.db.models.chat import QueryRun
from gaia.db.models.profiling import ColumnProfile, VerificationTest, ViewColumnLineage
from gaia.db.models.semantics import (
    ColumnSemantic,
    EvidenceItem,
    GlossaryTerm,
    JoinHint,
    ReviewItem,
    TableSemantic,
)
from gaia.semantics.evidence import PERSONAL_TYPES, shape
from gaia.semantics.schemas import ROLES

__all__ = ["ACTIONS", "ReviewError", "ReviewNotFound", "ReviewService"]

ACTIONS = ("accept", "correct", "reject", "unused")
EDITABLE = ("business_name", "purpose", "role", "entity", "unit", "synonyms", "personal")
TABLE_EDITABLE = ("business_name", "purpose", "grain", "entity", "synonyms")
_SKIP = {"id", "version", "is_current", "created_at", "search_vector", "reviewed_by",
         "reviewed_at", "review_note"}
_PERSONAL_MIN_RATE = 0.5


class ReviewError(RuntimeError):
    """A decision that cannot be taken as asked (400)."""


class ReviewNotFound(ReviewError):
    """No such data source, column, table or hint here (404)."""


def _copy(row, model) -> dict[str, Any]:
    return {c.key: getattr(row, c.key) for c in model.__table__.columns if c.key not in _SKIP}


@dataclass
class Reviewer:
    login: str
    pii_processor: bool = False


class ReviewService:
    def __init__(self, settings: Settings, session_factory, *, now=None) -> None:
        self.settings = settings
        self.factory = session_factory
        self.now = now or (lambda: datetime.now(UTC))
        self.blind_share = settings.semantics.review_blind_share  # P6-5

    # -- helpers ---------------------------------------------------------------------------
    async def _source(self, s: AsyncSession, name: str) -> DataSource:
        ds = await s.scalar(select(DataSource).where(DataSource.name == name))
        if ds is None or ds.status == "tombstoned":
            raise ReviewNotFound(f"no data source {name!r}")
        return ds

    async def _column(self, s: AsyncSession, ds: DataSource, column_id: uuid.UUID
                      ) -> tuple[ColumnRecord, TableRecord]:
        row = (await s.execute(select(ColumnRecord, TableRecord).join(
            TableRecord, TableRecord.id == ColumnRecord.table_id).where(
            ColumnRecord.id == column_id, TableRecord.data_source_id == ds.id))).first()
        if row is None:
            raise ReviewNotFound("no such column in this data source")
        return row[0], row[1]

    async def _current(self, s: AsyncSession, column_id: uuid.UUID) -> ColumnSemantic | None:
        return await s.scalar(select(ColumnSemantic).where(
            ColumnSemantic.column_id == column_id, ColumnSemantic.is_current.is_(True)))

    async def _snapshot_table(self, s: AsyncSession, ds: DataSource, table: TableRecord):
        from gaia.db.repositories.semantics_store import SemanticsStore
        from gaia.introspect.service import snapshot_from_document

        record = await SemanticsStore(s).latest_snapshot(ds.id)
        if record is None:
            return None
        snap = snapshot_from_document(record.document)
        return next((t for t in snap.tables if t.schema == table.schema_name
                     and t.name == table.object_name), None)

    # -- what the SME sees -----------------------------------------------------------------
    async def sources(self, data_source_ids: set[str]) -> list[dict[str, Any]]:
        """The data sources this SME reviews, with the size of their queues."""
        async with self.factory() as s:
            out = []
            for ds in (await s.scalars(select(DataSource).where(DataSource.id.in_(
                    [uuid.UUID(i) for i in data_source_ids] or [uuid.UUID(int=0)]))
                    .order_by(DataSource.name))).all():
                open_ = await s.scalar(select(func.count()).select_from(ReviewItem).where(
                    ReviewItem.data_source_id == ds.id, ReviewItem.status == "open"))
                reviewed = await s.scalar(select(func.count()).select_from(ColumnSemantic).join(
                    TableRecord, TableRecord.id == ColumnSemantic.table_id).where(
                    TableRecord.data_source_id == ds.id, ColumnSemantic.is_current.is_(True),
                    ColumnSemantic.reviewed_by.is_not(None),
                    ColumnSemantic.status.in_(("sme_reviewed", "sme_rejected"))))
                stale = await s.scalar(select(func.count()).select_from(
                    _stale_decisions(ds.id).subquery()))
                out.append({"name": ds.name, "id": str(ds.id), "dialect": ds.dialect,
                            "open": open_, "reviewed": reviewed, "stale": stale})
            return out

    async def queue(self, data_source: str, *, status: str = "open", reason: str | None = None,
                    table: str | None = None, limit: int = 50, offset: int = 0
                    ) -> dict[str, Any]:
        """Items highest priority first. ``status``: ``open`` (to do), ``done`` (decided),
        ``stale`` (decisions whose column changed since)."""
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            if status == "stale":
                q = _stale_decisions(ds.id)
                if table:
                    q = q.where(func.lower(TableRecord.schema_name + "." + TableRecord.object_name)
                                == table.lower())
                total = await s.scalar(select(func.count()).select_from(q.subquery()))
                rows = (await s.execute(q.order_by(ColumnSemantic.reviewed_at.desc())
                                        .limit(limit).offset(offset))).all()
                return {"total": total, "items": [
                    _item(None, t, c, sem, self.blind_share) for sem, c, t in rows]}
            wanted = ("open",) if status == "open" else ("done", "dismissed")
            base = (select(ReviewItem, TableRecord, ColumnRecord, ColumnSemantic)
                    .join(TableRecord, TableRecord.id == ReviewItem.table_id)
                    .outerjoin(ColumnRecord, ColumnRecord.id == ReviewItem.column_id)
                    .outerjoin(ColumnSemantic, and_(
                        ColumnSemantic.column_id == ReviewItem.column_id,
                        ColumnSemantic.is_current.is_(True)))
                    .where(ReviewItem.data_source_id == ds.id, ReviewItem.status.in_(wanted)))
            if reason:
                base = base.where(ReviewItem.reasons.contains([reason]))
                if self.blind_share > 0:  # a filter on why would tell a blind item's certainty
                    base = base.where(~and_(_blind_sql(self.blind_share),
                                            ColumnSemantic.reviewed_by.is_(None)))
            if table:
                base = base.where(func.lower(TableRecord.schema_name + "."
                                             + TableRecord.object_name) == table.lower())
            total = await s.scalar(select(func.count()).select_from(base.subquery()))
            order = (ReviewItem.priority.desc(), ReviewItem.id) if status == "open" else \
                (ReviewItem.resolved_at.desc(),)
            rows = (await s.execute(base.order_by(*order).limit(limit).offset(offset))).all()
            return {"total": total, "items": [_item(i, t, c, sem, self.blind_share)
                                              for i, t, c, sem in rows]}

    async def column(self, data_source: str, column_id: uuid.UUID, who: Reviewer
                     ) -> dict[str, Any]:
        """Everything Gaia knows about one column (P5-4: summarised, with a dive-in)."""
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            col, table = await self._column(s, ds, column_id)
            sem = await self._current(s, column_id)
            tsem = await s.scalar(select(TableSemantic).where(
                TableSemantic.table_id == table.id, TableSemantic.is_current.is_(True)))
            snap = await self._snapshot_table(s, ds, table)
            facts = _facts(snap, col.column_name) if snap else {}
            ledger = [] if sem is None else [
                {"kind": e.kind, "direction": e.direction, "strength": e.strength,
                 "source": e.source, "ref": e.ref, "detail": e.detail}
                for e in (await s.scalars(select(EvidenceItem).where(
                    EvidenceItem.column_semantic_id == sem.id).order_by(
                    EvidenceItem.direction, EvidenceItem.strength.desc()))).all()]
            profile_row = await s.scalar(select(ColumnProfile).where(
                ColumnProfile.table_id == table.id, ColumnProfile.column_name == col.column_name)
                .order_by(ColumnProfile.created_at.desc()).limit(1))
            profile, personal = _profile(profile_row.summary if profile_row else None,
                                         sem.semantic_type if sem else None, who.pii_processor)
            tests = [{"test": t.test_id, "kind": t.kind, "passed": t.passed, "metric": t.metric,
                      "origin": t.origin, "method": t.method, "detail": t.detail}
                     for t in (await s.scalars(select(VerificationTest).where(
                         VerificationTest.table_id == table.id,
                         VerificationTest.column_name == col.column_name,
                         VerificationTest.is_current.is_(True)))).all()]
            hints = [_hint(h) for h in (await s.scalars(select(JoinHint).where(
                JoinHint.data_source_id == ds.id, or_(
                    and_(JoinHint.from_schema == table.schema_name,
                         JoinHint.from_table == table.object_name,
                         JoinHint.from_columns.contains([col.column_name])),
                    and_(JoinHint.to_schema == table.schema_name,
                         JoinHint.to_table == table.object_name,
                         JoinHint.to_columns.contains([col.column_name]))))
                .order_by(JoinHint.source, JoinHint.status))).all()]
            lineage = await self._lineage(s, ds, table, col.column_name)
            versions = [{"version": v.version, "created_at": v.created_at, "status": v.status,
                         "band": v.band, "score": round(v.calibrated_score, 3),
                         "business_name": v.business_name, "role": v.role, "model": v.model,
                         "reviewed_by": v.reviewed_by, "review_note": v.review_note}
                        for v in (await s.scalars(select(ColumnSemantic).where(
                            ColumnSemantic.column_id == column_id).order_by(
                            ColumnSemantic.version.desc()).limit(20))).all()]
            item = await s.scalar(select(ReviewItem).where(
                ReviewItem.column_id == column_id).order_by(ReviewItem.created_at.desc())
                .limit(1))
            questions = await self._questions(s, ds, table, col.column_name)
            meaning = _meaning(sem) if sem else None
            blind = _blind(col, sem, self.blind_share)
            if blind and meaning is not None:  # P6-5: Gaia's certainty is not shown
                meaning.update(score=None, raw_score=None, band=None, self_confidence=None,
                               critique=None)
                versions = [{**v, "score": None, "band": None} if not v["reviewed_by"] else v
                            for v in versions]
            return {
                "blind": blind,
                "data_source": ds.name,
                "table": {"id": str(table.id), "schema": table.schema_name,
                          "name": table.object_name, "kind": table.kind,
                          "business_name": tsem.business_name if tsem else None,
                          "purpose": tsem.purpose if tsem else None,
                          "grain": tsem.grain if tsem else None,
                          "status": tsem.status if tsem else None},
                "column": {"id": str(col.id), "name": col.column_name, **facts},
                "meaning": meaning,
                "summary": [x for x in _summary(meaning, ledger, tests, profile, personal)
                            if not (blind and x["key"] == "unsure")],  # doubt, in words
                "breakdown": sem.score_breakdown if sem and not blind else None,
                "ledger": ledger, "profile": profile, "personal": personal,
                "masked": personal and not who.pii_processor,
                "tests": tests, "join_hints": hints, "lineage": lineage,
                "versions": versions, "questions": questions,
                "review": None if item is None else {
                    "id": str(item.id), "status": item.status,
                    "reasons": ["blind"] if blind else list(item.reasons),
                    "priority": round(item.priority, 4), "importance": item.importance,
                    "coverage": item.coverage, "assignee": item.assignee},
                "reviewable": meaning is not None and not meaning["inherited_from"],
                "roles": list(ROLES),
            }

    async def tables(self, data_source: str) -> list[dict[str, Any]]:
        """Every live table and view of the data source, with Gaia's name for it and how many
        of its columns wait for review or were decided — a way in besides the queue."""
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            rows = (await s.execute(select(TableRecord, TableSemantic).outerjoin(
                TableSemantic, and_(TableSemantic.table_id == TableRecord.id,
                                    TableSemantic.is_current.is_(True))).where(
                TableRecord.data_source_id == ds.id, TableRecord.is_tombstoned.is_(False))
                .order_by(TableRecord.schema_name, TableRecord.object_name))).all()
            queued = dict((await s.execute(select(ReviewItem.table_id, func.count()).where(
                ReviewItem.data_source_id == ds.id, ReviewItem.status == "open")
                .group_by(ReviewItem.table_id))).all())
            decided = dict((await s.execute(select(ColumnSemantic.table_id, func.count()).where(
                ColumnSemantic.is_current.is_(True), ColumnSemantic.reviewed_by.is_not(None),
                ColumnSemantic.table_id.in_([t.id for t, _ in rows] or [uuid.UUID(int=0)]))
                .group_by(ColumnSemantic.table_id))).all())
            columns = dict((await s.execute(select(ColumnRecord.table_id, func.count()).where(
                ColumnRecord.table_id.in_([t.id for t, _ in rows] or [uuid.UUID(int=0)]),
                ColumnRecord.is_tombstoned.is_(False)).group_by(ColumnRecord.table_id))).all())
            return [{"id": str(t.id), "name": f"{t.schema_name}.{t.object_name}", "kind": t.kind,
                     "business_name": sem.business_name if sem else None,
                     "reviewed_by": sem.reviewed_by if sem else None,
                     "columns": columns.get(t.id, 0), "queued": queued.get(t.id, 0),
                     "decided": decided.get(t.id, 0)} for t, sem in rows]

    async def table(self, data_source: str, table_id: uuid.UUID) -> dict[str, Any]:
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            table = await s.get(TableRecord, table_id)
            if table is None or table.data_source_id != ds.id:
                raise ReviewNotFound("no such table in this data source")
            tsem = await s.scalar(select(TableSemantic).where(
                TableSemantic.table_id == table.id, TableSemantic.is_current.is_(True)))
            cols = (await s.execute(select(ColumnRecord, ColumnSemantic).outerjoin(
                ColumnSemantic, and_(ColumnSemantic.column_id == ColumnRecord.id,
                                     ColumnSemantic.is_current.is_(True))).where(
                ColumnRecord.table_id == table.id, ColumnRecord.is_tombstoned.is_(False))
                .order_by(ColumnRecord.ordinal))).all()
            open_items = {i.column_id: i for i in (await s.scalars(select(ReviewItem).where(
                ReviewItem.table_id == table.id, ReviewItem.status == "open"))).all()}
            share = self.blind_share
            return {
                "data_source": ds.name, "id": str(table.id), "schema": table.schema_name,
                **(await _category_of(s, table.id)),
                "name": table.object_name, "kind": table.kind,
                "rows": table.estimated_rows,
                "meaning": None if tsem is None else {
                    "version": tsem.version, "status": tsem.status,
                    "business_name": tsem.business_name, "purpose": tsem.purpose,
                    "grain": tsem.grain, "entity": tsem.entity, "synonyms": list(tsem.synonyms),
                    "assumptions": list(tsem.assumptions), "unknowns": list(tsem.unknowns),
                    "score": tsem.calibrated_score, "reviewed_by": tsem.reviewed_by,
                    "reviewed_at": tsem.reviewed_at, "review_note": tsem.review_note,
                    "critique": tsem.critique},
                "columns": [{"id": str(c.id), "name": c.column_name, "data_type": c.data_type,
                             "business_name": sem.business_name if sem else None,
                             "role": sem.role if sem else None,
                             "score": round(sem.calibrated_score, 3)
                             if sem and not _blind(c, sem, share) else None,
                             "band": sem.band if sem and not _blind(c, sem, share) else None,
                             "status": sem.status if sem else None,
                             "inherited": bool(sem and (sem.score_breakdown or {}).get(
                                 "inherited_from")),
                             "queued": c.id in open_items,
                             "blind": _blind(c, sem, share),
                             "reasons": (["blind"] if _blind(c, sem, share) else
                                         list(open_items[c.id].reasons))
                             if c.id in open_items else []} for c, sem in cols],
            }

    async def _lineage(self, s: AsyncSession, ds: DataSource, table: TableRecord,
                       column: str) -> dict[str, Any]:
        exact = f"{table.schema_name}.{table.object_name}.{column}"
        used = (await s.scalars(select(ViewColumnLineage).where(
            ViewColumnLineage.data_source_id == ds.id, or_(
                func.lower(ViewColumnLineage.source) == exact.lower(),
                ViewColumnLineage.inputs.contains([exact]),
                ViewColumnLineage.inputs.contains([exact.lower()]))))).all()
        own = (await s.scalars(select(ViewColumnLineage).where(
            ViewColumnLineage.data_source_id == ds.id,
            ViewColumnLineage.view_schema == table.schema_name,
            ViewColumnLineage.view_name == table.object_name,
            ViewColumnLineage.column_name == column))).all()
        return {
            "shown_in": [{"view": f"{v.view_schema}.{v.view_name}", "column": v.column_name,
                          "how": v.classification} for v in used],
            "comes_from": [{"how": v.classification, "source": v.source,
                            "expression": v.expression, "inputs": list(v.inputs or [])}
                           for v in own],
        }

    async def _questions(self, s: AsyncSession, ds: DataSource, table: TableRecord,
                         column: str, limit: int = 10) -> list[dict[str, Any]]:
        """Questions whose query read this table and names this column (approximate: the
        guard does not list the columns it read yet — 5C will)."""
        key = f"{table.schema_name}.{table.object_name}"
        rows = (await s.scalars(select(QueryRun).where(
            QueryRun.data_source_id == ds.id, QueryRun.question.is_not(None),
            QueryRun.status == "ok", QueryRun.semantic_versions.has_key(key),
            QueryRun.executed_sql.ilike(f"%{column}%"))
            .order_by(QueryRun.created_at.desc()).limit(limit))).all()
        return [{"question": r.question, "at": r.created_at, "origin": r.origin} for r in rows]


    # -- decisions ---------------------------------------------------------------------------
    async def decide_column(self, data_source: str, column_id: uuid.UUID, action: str,
                            who: Reviewer, *, note: str | None = None,
                            changes: dict[str, Any] | None = None,
                            value_meanings: dict[str, str] | None = None) -> dict[str, Any]:
        """Record an SME's decision as a new, signed version; close the review item; travel
        where lineage is certain, re-queue where it is not. Returns the new meaning and the
        next open item."""
        if action not in ACTIONS:
            raise ReviewError(f"unknown action {action!r} ({', '.join(ACTIONS)})")
        note = (note or "").strip() or None
        if action == "reject" and not note:
            raise ReviewError("a rejection needs a note: what is wrong with Gaia's meaning")
        now = self.now()
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            col, table = await self._column(s, ds, column_id)
            cur = await self._current(s, column_id)
            if cur is None:
                raise ReviewError("Gaia has no meaning for this column yet: reason the table "
                                  "first (make reason)")
            base = (cur.score_breakdown or {}).get("inherited_from")
            if base:
                raise ReviewError(f"this view column shows {base} as it is: review that column, "
                                  "and the decision comes here by itself")
            row = _copy(cur, ColumnSemantic)
            row.update(status="sme_reviewed", band="accepted", raw_score=1.0,
                       calibrated_score=1.0, coverage="sme", reviewed_by=who.login,
                       reviewed_at=now, review_note=note)
            if action == "correct":
                row.update(_edits(changes or {}, cur))
                if value_meanings is not None:
                    row["value_domain"] = _with_meanings(cur.value_domain or {}, value_meanings)
            elif value_meanings or changes:
                raise ReviewError("changes go with the action 'correct'")
            if action == "reject":
                row.update(status="sme_rejected", band="abstain", raw_score=0.0,
                           calibrated_score=0.0)
            if action == "unused":
                row["unused"] = True
            row["score_breakdown"] = {
                "sme": {"action": action, "reviewer": who.login, "at": now.isoformat(),
                        "note": note,
                        # decided without Gaia's certainty on screen (P6-5)
                        "blind": bool(not cur.reviewed_by
                                      and is_blind(column_id, self.blind_share))},
                "previous": {"version": cur.version, "status": cur.status, "band": cur.band,
                             "raw_score": cur.raw_score,
                             "calibrated_score": cur.calibrated_score},
                # kept so a re-score can still explain the machine's part
                "machine": {k: v for k, v in (cur.score_breakdown or {}).items()
                            if k not in ("sme", "previous", "machine")},
            }
            version = (await s.scalar(select(func.max(ColumnSemantic.version)).where(
                ColumnSemantic.column_id == column_id)) or 0) + 1
            await s.execute(update(ColumnSemantic).where(
                ColumnSemantic.column_id == column_id, ColumnSemantic.is_current.is_(True))
                .values(is_current=False).execution_options(synchronize_session=False))
            new = ColumnSemantic(**row, version=version, is_current=True)
            s.add(new)
            await s.flush()
            # the ledger: the machine's evidence, plus the SME's (the strongest there is)
            for e in (await s.scalars(select(EvidenceItem).where(
                    EvidenceItem.column_semantic_id == cur.id))).all():
                s.add(EvidenceItem(column_semantic_id=new.id, table_id=e.table_id, kind=e.kind,
                                   direction=e.direction, strength=e.strength, source=e.source,
                                   ref=e.ref, detail=e.detail))
            s.add(EvidenceItem(
                column_semantic_id=new.id, table_id=table.id, kind=f"sme_{action}",
                direction="contradicts" if action == "reject" else "supports", strength=1.0,
                source="sme", ref=f"sme:{who.login}",
                detail=(note or action) + (f"; changed: {', '.join(sorted(changes or {}))}"
                                           if changes else "")))
            await s.execute(update(ReviewItem).where(
                ReviewItem.column_id == column_id, ReviewItem.status == "open").values(
                status="done", resolved_at=now, assignee=who.login,
                column_semantic_id=new.id).execution_options(synchronize_session=False))
            requeued = await self._requeue_computed(s, ds, table, col.column_name, now)
            from gaia.auth import audit

            await audit.record(s, "review_decision", actor=who.login,
                               subject=f"{ds.name}:{table.schema_name}.{table.object_name}."
                                       f"{col.column_name}",
                               detail={"action": action, "version": version,
                                       "changed": sorted(changes or {})
                                       + (["value_meanings"] if value_meanings else [])})
            await s.commit()
            meaning = _meaning(new)
        propagated = await self._propagate(data_source, table, col.column_name)
        nxt = await self.queue(data_source, limit=1)
        return {"meaning": meaning, "propagated": propagated, "requeued": requeued,
                "next": nxt["items"][0] if nxt["items"] else None}

    async def _requeue_computed(self, s: AsyncSession, ds: DataSource, table: TableRecord,
                                column: str, now: datetime) -> list[str]:
        """Computed view columns that use this column are not copied to — they are queued
        for a look (``input_reviewed``; an open item gets the reason added), unless an SME
        has decided them already."""
        exact = f"{table.schema_name}.{table.object_name}.{column}"
        rows = (await s.scalars(select(ViewColumnLineage).where(
            ViewColumnLineage.data_source_id == ds.id,
            ViewColumnLineage.classification != "pass_through",
            or_(ViewColumnLineage.inputs.contains([exact]),
                ViewColumnLineage.inputs.contains([exact.lower()]))))).all()
        out = []
        for v in rows:
            pair = (await s.execute(select(ColumnRecord, ColumnSemantic).join(
                TableRecord, TableRecord.id == ColumnRecord.table_id).outerjoin(
                ColumnSemantic, and_(ColumnSemantic.column_id == ColumnRecord.id,
                                     ColumnSemantic.is_current.is_(True))).where(
                TableRecord.data_source_id == ds.id, TableRecord.schema_name == v.view_schema,
                TableRecord.object_name == v.view_name,
                ColumnRecord.column_name == v.column_name))).first()
            if pair is None:
                continue
            vcol, vsem = pair
            if vsem is not None and vsem.reviewed_by:
                continue
            queued = await s.scalar(select(ReviewItem).where(
                ReviewItem.column_id == vcol.id, ReviewItem.status == "open"))
            if queued is not None:  # already waiting: say why it matters more now
                if "input_reviewed" not in queued.reasons:
                    queued.reasons = [*queued.reasons, "input_reviewed"]
                out.append(f"{v.view_schema}.{v.view_name}.{v.column_name}")
                continue
            score = vsem.calibrated_score if vsem else 0.0
            s.add(ReviewItem(data_source_id=ds.id, table_id=vcol.table_id, column_id=vcol.id,
                             column_semantic_id=vsem.id if vsem else None,
                             subject_kind="column", reasons=["input_reviewed"],
                             priority=round(0.5 * (1 - score), 4), score=score,
                             importance=0.5, coverage=vsem.coverage if vsem else "catalog",
                             status="open", created_at=now))
            out.append(f"{v.view_schema}.{v.view_name}.{v.column_name}")
        await s.flush()
        return out

    async def _propagate(self, data_source: str, table: TableRecord, column: str
                         ) -> dict[str, Any]:
        """Pass-through view columns take the decision at once: a re-score of the views that
        show this column (no model call). If a reasoning run holds the data source, the next
        run does it instead."""
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            exact = f"{table.schema_name}.{table.object_name}.{column}"
            views = sorted({f"{v.view_schema}.{v.view_name}" for v in (await s.scalars(
                select(ViewColumnLineage).where(
                    ViewColumnLineage.data_source_id == ds.id,
                    ViewColumnLineage.classification == "pass_through",
                    func.lower(ViewColumnLineage.source) == exact.lower()))).all()})
        if not views:
            return {"views": [], "done": True}
        from gaia.semantics.service import ReasoningBusy, ReasoningOptions, run_reasoning

        try:
            async with self.factory() as s:
                await run_reasoning(self.settings, s, ReasoningOptions(
                    target=data_source, tables=views, rescore_only=True, embed=False),
                    llm_factory=_no_model)
        except ReasoningBusy:
            return {"views": views, "done": False,
                    "note": "a reasoning run holds the data source: the next run carries it"}
        return {"views": views, "done": True}

    async def decide_table(self, data_source: str, table_id: uuid.UUID, action: str,
                           who: Reviewer, *, note: str | None = None,
                           changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """The table's own meaning — its name, purpose and grain — reviewed on the same
        screen as its columns (P5-4). ``accept`` or ``correct``."""
        if action not in ("accept", "correct"):
            raise ReviewError("a table's meaning is accepted or corrected")
        now = self.now()
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            table = await s.get(TableRecord, table_id)
            if table is None or table.data_source_id != ds.id:
                raise ReviewNotFound("no such table in this data source")
            cur = await s.scalar(select(TableSemantic).where(
                TableSemantic.table_id == table.id, TableSemantic.is_current.is_(True)))
            if cur is None:
                raise ReviewError("Gaia has no meaning for this table yet (make reason)")
            row = _copy(cur, TableSemantic)
            if action == "correct":
                unknown = set(changes or {}) - set(TABLE_EDITABLE)
                if unknown:
                    raise ReviewError(f"not editable: {', '.join(sorted(unknown))}")
                for k, v in (changes or {}).items():
                    row[k] = _clean(k, v)
            row.update(status="sme_reviewed", reviewed_by=who.login, reviewed_at=now,
                       review_note=(note or "").strip() or None)
            version = (await s.scalar(select(func.max(TableSemantic.version)).where(
                TableSemantic.table_id == table.id)) or 0) + 1
            await s.execute(update(TableSemantic).where(
                TableSemantic.table_id == table.id, TableSemantic.is_current.is_(True))
                .values(is_current=False).execution_options(synchronize_session=False))
            new = TableSemantic(**row, version=version, is_current=True)
            s.add(new)
            await s.flush()
            await s.execute(update(ColumnSemantic).where(
                ColumnSemantic.table_id == table.id, ColumnSemantic.is_current.is_(True))
                .values(table_semantic_id=new.id).execution_options(synchronize_session=False))
            from gaia.auth import audit

            await audit.record(s, "review_decision", actor=who.login,
                               subject=f"{ds.name}:{table.schema_name}.{table.object_name}",
                               detail={"action": action, "version": version, "table": True,
                                       "changed": sorted(changes or {})})
            await s.commit()
        return await self.table(data_source, table_id)

    async def decide_hint(self, data_source: str, hint_id: uuid.UUID, action: str,
                          who: Reviewer, *, note: str | None = None) -> dict[str, Any]:
        """A join the model proposed, confirmed or rejected by the SME. An SME's hint is
        never overwritten by the model (D16); a confirmed one is a join path the guard
        accepts, a rejected one never is."""
        if action not in ("confirm", "reject"):
            raise ReviewError("a join hint is confirmed or rejected")
        if action == "reject" and not (note or "").strip():
            raise ReviewError("a rejection needs a note: what is wrong with this join")
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            h = await s.get(JoinHint, hint_id)
            if h is None or h.data_source_id != ds.id:
                raise ReviewNotFound("no such join hint in this data source")
            if h.source == "fk":
                raise ReviewError("a declared foreign key is a fact of the database, not a "
                                  "hint")
            h.source, h.status = "sme", ("confirmed" if action == "confirm" else "rejected")
            h.rationale = (f"{who.login}: {note}" if note else f"{action}ed by {who.login}")
            from gaia.auth import audit

            await audit.record(s, "review_decision", actor=who.login, subject=f"{ds.name}:"
                               f"{h.from_schema}.{h.from_table} -> {h.to_schema}.{h.to_table}",
                               detail={"action": action, "join_hint": str(h.id)})
            await s.commit()
            return _hint(h)

    async def glossary(self, data_source: str) -> list[dict[str, Any]]:
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            rows = (await s.scalars(select(GlossaryTerm).where(
                or_(GlossaryTerm.data_source_id == ds.id, GlossaryTerm.data_source_id.is_(None)),
                GlossaryTerm.status == "approved").order_by(GlossaryTerm.term))).all()
            return [{"id": str(g.id), "term": g.term, "definition": g.definition,
                     "synonyms": list(g.synonyms), "source": g.source,
                     "everywhere": g.data_source_id is None,
                     "editable": g.data_source_id == ds.id}
                    for g in rows]

    async def add_term(self, data_source: str, term: str, definition: str, who: Reviewer,
                       synonyms: list[str] | None = None) -> dict[str, Any]:
        """A business term the SME defines for this data source (D61 c: definitions come
        from people, and reach the SQL generator when a question uses the term)."""
        term, definition = (term or "").strip(), (definition or "").strip()
        if not term or not definition:
            raise ReviewError("a term needs a name and a definition")
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            g = await s.scalar(select(GlossaryTerm).where(
                GlossaryTerm.data_source_id == ds.id,
                GlossaryTerm.locale == self.settings.app.locale,
                func.lower(GlossaryTerm.term) == term.lower()))
            clean = [x.strip() for x in synonyms or [] if x.strip()]
            if g is not None and g.status == "approved":
                raise ReviewError(f"{term!r} is defined already")
            if g is not None:  # withdrawn before: defined again
                g.term, g.definition, g.synonyms = term, definition, clean
                g.status, g.source = "approved", "sme"
            else:
                g = GlossaryTerm(data_source_id=ds.id, term=term, definition=definition,
                                 synonyms=clean, locale=self.settings.app.locale, source="sme",
                                 status="approved")
                s.add(g)
            await s.flush()
            from gaia.auth import audit

            await audit.record(s, "glossary_term_added", actor=who.login,
                               subject=f"{ds.name}:{term}", detail={"synonyms": g.synonyms})
            await s.commit()
        return {"id": str(g.id), "term": term, "definition": definition,
                "synonyms": g.synonyms}

    async def change_term(self, data_source: str, term_id: uuid.UUID, who: Reviewer, *,
                          definition: str | None = None, synonyms: list[str] | None = None,
                          remove: bool = False) -> dict[str, Any]:
        """Edit a term of this data source, or withdraw it (kept, status ``rejected``: it
        stops reaching the SQL generator). Every change is audited."""
        async with self.factory() as s:
            ds = await self._source(s, data_source)
            g = await s.get(GlossaryTerm, term_id)
            if g is None or g.data_source_id != ds.id or g.status != "approved":
                raise ReviewNotFound("no such term in this data source")
            before = {"definition": g.definition, "synonyms": list(g.synonyms)}
            if remove:
                g.status = "rejected"
            else:
                if definition is not None:
                    if not definition.strip():
                        raise ReviewError("a term needs a definition")
                    g.definition = definition.strip()
                if synonyms is not None:
                    g.synonyms = [x.strip() for x in synonyms if x.strip()]
            from gaia.auth import audit

            await audit.record(s, "glossary_term_removed" if remove else
                               "glossary_term_changed", actor=who.login,
                               subject=f"{ds.name}:{g.term}", detail={"before": before})
            await s.commit()
            return {"id": str(g.id), "term": g.term, "definition": g.definition,
                    "synonyms": list(g.synonyms), "status": g.status}



async def _category_of(s: AsyncSession, table_id: uuid.UUID) -> dict[str, Any]:
    """The table's data category in force and the categories an SME may choose (5C)."""
    from gaia.db.models.auth import Category, ObjectCategory

    row = (await s.execute(select(ObjectCategory, Category).join(
        Category, Category.id == ObjectCategory.category_id).where(
        ObjectCategory.table_id == table_id, ObjectCategory.is_current.is_(True)))).first()
    options = (await s.scalars(select(Category).where(Category.retired_at.is_(None))
                               .order_by(func.lower(Category.name)))).all()
    return {"category": None if row is None else {
                "id": str(row[1].id), "name": row[1].name, "status": row[0].status,
                "source": row[0].source, "decided_by": row[0].decided_by,
                "decided_at": row[0].decided_at, "reason": row[0].reason},
            "categories": [{"id": str(c.id), "name": c.name, "description": c.description}
                           for c in options]}


def _stale_decisions(data_source_id: uuid.UUID):
    """Columns whose latest SME decision went stale (their structure changed since) — current
    or already replaced by the machine's new meaning: either way a person has to look again."""
    latest = (select(ColumnSemantic.column_id, func.max(ColumnSemantic.version).label("v"))
              .where(ColumnSemantic.reviewed_by.is_not(None))
              .group_by(ColumnSemantic.column_id).subquery())
    return (select(ColumnSemantic, ColumnRecord, TableRecord)
            .join(latest, and_(latest.c.column_id == ColumnSemantic.column_id,
                               latest.c.v == ColumnSemantic.version))
            .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
            .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
            .where(TableRecord.data_source_id == data_source_id,
                   ColumnSemantic.status == "stale"))

def _no_model():
    raise ReviewError("a re-score needs no model")


def _clean(key: str, value: Any) -> Any:
    if key == "personal":  # 5C: True, False, or None (the detectors decide again)
        if value is None or isinstance(value, bool):
            return value
        raise ReviewError("personal is true, false or null")
    if key == "synonyms":
        values = value if isinstance(value, list) else str(value or "").split(",")
        return [str(v).strip() for v in values if str(v).strip()]
    text = str(value or "").strip()
    if key in ("business_name", "purpose") and not text:
        raise ReviewError(f"{key} cannot be empty")
    return text or None


def _edits(changes: dict[str, Any], cur: ColumnSemantic) -> dict[str, Any]:
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ReviewError(f"not editable: {', '.join(sorted(unknown))}")
    out = {k: _clean(k, v) for k, v in changes.items()}
    if "role" in out and out["role"] not in ROLES:
        raise ReviewError(f"role must be one of {', '.join(ROLES)}")
    return out


def _with_meanings(domain: dict[str, Any], meanings: dict[str, str]) -> dict[str, Any]:
    clean = {str(k).strip(): str(v).strip() for k, v in meanings.items()
             if str(k).strip() and str(v).strip()}
    values = list(domain.get("values") or [])
    values += [v for v in clean if v not in values]
    return {**domain, "kind": "enumeration" if clean else domain.get("kind", "unknown"),
            "values": values, "meanings": clean, "source": "sme"}


# -- views of rows ---------------------------------------------------------------------------


def is_blind(column_id: Any, share: float) -> bool:
    """P6-5 (D83): whether a column's undecided meaning is shown without Gaia's certainty —
    a fixed share of columns, chosen by a hash of the column (the same on every screen and
    every visit), so that people's decisions can be compared with and without it."""
    if not column_id or share <= 0:
        return False
    return int(hashlib.sha256(str(column_id).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < share


def _blind_sql(share: float):
    """``is_blind`` in SQL, on the review item's column (the same hash: PostgreSQL's sha256 of
    the id as text, its first 32 bits)."""
    return sa.text("('x' || substr(encode(sha256(convert_to(semantics.review_item.column_id"
                   "::text, 'UTF8')), 'hex'), 1, 8))::bit(32)::bigint / 4294967295.0 < :share"
                   ).bindparams(share=share)


def _blind(c: ColumnRecord | None, sem: ColumnSemantic | None, share: float) -> bool:
    return bool(c is not None and sem is not None and not sem.reviewed_by
                and is_blind(c.id, share))


def _item(item: ReviewItem | None, t: TableRecord, c: ColumnRecord | None,
          sem: ColumnSemantic | None, share: float = 0.0) -> dict[str, Any]:
    if _blind(c, sem, share):  # P6-5: no score, band or "Gaia is not sure"
        return {**_item(item, t, c, sem), "score": None, "band": None, "reasons": ["blind"],
                "blind": True}
    return {
        "id": str(item.id) if item else None,
        "table_id": str(t.id), "table": f"{t.schema_name}.{t.object_name}",
        "column_id": str(c.id) if c else None, "column": c.column_name if c else None,
        "data_type": c.data_type if c else None,
        "business_name": sem.business_name if sem else None,
        "role": sem.role if sem else None,
        "score": round(sem.calibrated_score, 3) if sem else (item.score if item else None),
        "band": sem.band if sem else None, "status": sem.status if sem else None,
        "reasons": list(item.reasons) if item else ["stale_decision"],
        "priority": round(item.priority, 4) if item else None,
        "coverage": item.coverage if item else None,
        "reviewed_by": sem.reviewed_by if sem else None,
        "reviewed_at": sem.reviewed_at if sem else None,
        "resolved_at": item.resolved_at if item else None,
    }


def _meaning(sem: ColumnSemantic) -> dict[str, Any]:
    return {
        "version": sem.version, "status": sem.status, "band": sem.band,
        "score": round(sem.calibrated_score, 3), "raw_score": round(sem.raw_score, 3),
        "business_name": sem.business_name, "purpose": sem.purpose, "role": sem.role,
        "entity": sem.entity, "unit": sem.unit, "value_domain": sem.value_domain,
        "value_meanings": (sem.value_domain or {}).get("meanings") or {},
        "synonyms": list(sem.synonyms), "example_questions": list(sem.example_questions or []),
        "assumptions": list(sem.assumptions), "unknowns": list(sem.unknowns),
        "alternatives": list(sem.alternatives or []), "self_confidence": sem.self_confidence,
        "critique": sem.critique, "semantic_type": sem.semantic_type,
        "claimed_semantic_type": sem.claimed_semantic_type, "coverage": sem.coverage,
        "unused": sem.unused, "personal": sem.personal, "model": sem.model,
        "prompt_version": sem.prompt_version,
        "created_at": sem.created_at, "reviewed_by": sem.reviewed_by,
        "reviewed_at": sem.reviewed_at, "review_note": sem.review_note,
        "inherited_from": (sem.score_breakdown or {}).get("inherited_from"),
    }


def _hint(h: JoinHint) -> dict[str, Any]:
    return {"id": str(h.id), "from": f"{h.from_schema}.{h.from_table}({', '.join(h.from_columns)})",
            "to": f"{h.to_schema}.{h.to_table}({', '.join(h.to_columns)})",
            "source": h.source, "status": h.status, "confidence": h.confidence,
            "rationale": h.rationale, "reviewable": h.source != "fk"}


def _facts(table, column: str) -> dict[str, Any]:
    c = next((x for x in table.columns if x.name == column), None)
    if c is None:
        return {}
    keys = [k.kind for k in table.constraints if column in k.columns
            and k.kind in ("primary_key", "unique")]
    checks = [k.definition for k in table.constraints if k.kind == "check"
              and (column in k.columns or (k.definition and column in k.definition))]
    fks = [f"{r.to_schema}.{r.to_table}({', '.join(r.to_columns)})" for r in table.relations
           if column in r.from_columns]
    indexes = [i.name for i in table.indexes
               if any(ic.get("name") == column for ic in i.columns)]
    return {"data_type": c.data_type, "nullable": c.nullable, "default": c.default,
            "comment": c.comment, "ordinal": c.ordinal, "keys": keys, "checks": checks,
            "references": fks, "indexes": indexes, "computed": c.computed_definition,
            "type_definition": c.type_definition, "rows": table.estimated_rows}


def _profile(summary: dict[str, Any] | None, semantic_type: str | None, processor: bool
             ) -> tuple[dict[str, Any] | None, bool]:
    """The profile as the SME may see it: values of personal data as shapes unless the SME
    is a PII processor (D69)."""
    if not summary:
        return None, semantic_type in PERSONAL_TYPES
    detectors = summary.get("detectors") or []
    personal = semantic_type in PERSONAL_TYPES or any(
        d.get("semantic_type") in PERSONAL_TYPES and (d.get("match_rate") or 0)
        >= _PERSONAL_MIN_RATE for d in detectors)
    out = {k: summary.get(k) for k in (
        "method", "rows", "non_null", "distinct", "distinct_method", "minimum", "maximum",
        "min_length", "max_length", "avg_length", "numeric_parse_rate", "date_parse_rate",
        "granularity", "empty")}
    out["top_values"] = [list(v) for v in summary.get("top_values") or []]
    out["sample_values"] = list(summary.get("sample_values") or [])
    out["detectors"] = detectors
    if personal and not processor:
        out["top_values"] = [[shape(str(v)), n] for v, n in out["top_values"]]
        out["sample_values"] = [shape(str(v)) for v in out["sample_values"]]
        for k in ("minimum", "maximum"):
            if out[k] is not None:
                out[k] = shape(str(out[k]))
    return out, personal


def _summary(meaning: dict[str, Any] | None, ledger: list[dict[str, Any]],
             tests: list[dict[str, Any]], profile: dict[str, Any] | None,
             personal: bool) -> list[dict[str, Any]]:
    """A few plain sentences — what Gaia thinks and why, before the details — as keys and
    values the UI words from its locale file (``review.summary.<key>``)."""
    if meaning is None:
        return [{"key": "none"}]
    out: list[dict[str, Any]] = [{"key": "reads_as", "name": meaning["business_name"],
                                  "role": meaning["role"], "purpose": meaning["purpose"]}]
    if meaning["reviewed_by"]:
        out.append({"key": "decided", "status": meaning["status"],
                    "who": meaning["reviewed_by"]})
    elif meaning["score"] is None:
        out.append({"key": "blind"})  # P6-5
    else:
        out.append({"key": "score", "score": meaning["score"], "band": meaning["band"],
                    "supports": sum(1 for e in ledger if e["direction"] == "supports"),
                    "contradicts": sum(1 for e in ledger if e["direction"] == "contradicts")})
    crit = meaning.get("critique") or {}
    if crit.get("verdict"):
        out.append({"key": "critique", "verdict": crit["verdict"]})
    if tests:
        out.append({"key": "tests", "passed": sum(1 for t in tests if t["passed"]),
                    "total": len(tests)})
    if profile and profile.get("distinct") is not None:
        out.append({"key": "distinct", "count": profile["distinct"]})
    if personal:
        out.append({"key": "personal"})
    if meaning.get("unknowns"):
        out.append({"key": "unsure", "items": "; ".join(meaning["unknowns"][:3])})
    return out
