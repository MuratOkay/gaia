"""Loading the retrieval index and searching vectors (Phase 4B).

Read-only over Gaia's metadata store: the latest snapshot, the current table and
column meanings, the current profiles, the join paths (shared with the SQL
guard, ``query.catalog``) and the stored embeddings. Nothing touches the target.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from gaia.core.config import Settings
from gaia.dialects.types import type_info
from gaia.profiling.summary import ColumnProfileSummary
from gaia.query.catalog import load_guard_catalog
from gaia.query.retrieval import IndexedColumn, IndexedObject, RetrievalIndex, VectorHit
from gaia.semantics.evidence import PERSONAL_TYPES, detected_type

__all__ = ["load_retrieval_index", "vector_search"]

_PERSONAL_MIN_RATE = 0.80  # as the exposure policy (semantics.evidence): a weak detection counts
_EXCLUDED_BANDS = ("abstain",)
_EXCLUDED_STATUSES = ("sme_rejected", "stale")
_MAX_VALUES = 30


def _enum_labels(type_definition: str | None) -> list[str]:
    import re

    if not type_definition or not type_definition.lower().startswith("enum"):
        return []
    return [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", type_definition)]


def _values(dialect: str, data_type: str, type_definition: str | None,
            summary: ColumnProfileSummary | None, stated: list[str], raw: bool,
            code_max_distinct: int) -> tuple[list[str], str | None]:
    """Values the context may show, or a note why not (D24), in this order:
    enum labels (catalog facts); nothing for personal data unless values may go raw;
    the profiled values of a low-cardinality column; a distinct count for a text
    column with too many; else the values the model itself stated."""
    labels = _enum_labels(type_definition)
    if labels:
        return labels, None
    personal = summary is not None and any(
        d.semantic_type in PERSONAL_TYPES and d.match_rate >= _PERSONAL_MIN_RATE
        for d in summary.detectors)
    if personal and not raw:
        return [], "personal data: values not shown"
    info = type_info(dialect, data_type, type_definition)
    if summary is not None and summary.distinct is not None:
        listable = info.is_text or summary.distinct <= 10  # numbers only as small code sets
        if (summary.top_values and listable
                and summary.distinct <= min(code_max_distinct, _MAX_VALUES)):
            return [str(v) for v, _n in summary.top_values[:_MAX_VALUES]], None
        if summary.distinct > min(code_max_distinct, _MAX_VALUES):
            if info.is_text:
                return [], f"~{summary.distinct:,} distinct values"
            return [], None
        if not listable:
            return [], None
    if stated and len(stated) <= _MAX_VALUES:
        return [str(v) for v in stated], None
    return [], None


def _range(dialect: str, data_type: str, type_definition: str | None,
           summary: ColumnProfileSummary | None, semantic_type: str | None
           ) -> tuple[str, str] | None:
    if summary is None or summary.minimum is None or summary.maximum is None:
        return None
    info = type_info(dialect, data_type, type_definition)
    if info.is_temporal or semantic_type in ("date_int", "date_text", "datetime_text"):
        return str(summary.minimum)[:19], str(summary.maximum)[:19]
    return None


async def load_retrieval_index(session, data_source: str, settings: Settings) -> RetrievalIndex:
    from gaia.db.repositories.profile_store import ProfileStore
    from gaia.db.repositories.semantics_store import SemanticsStore
    from gaia.introspect.service import snapshot_from_document

    catalog, ds_id = await load_guard_catalog(session, data_source)
    store = SemanticsStore(session)
    record = await store.latest_snapshot(ds_id)
    snapshot = snapshot_from_document(record.document)
    tables = await store.live_tables(ds_id)
    ids = [rec.id for rec in tables.values()]
    table_sems = await store.current_table_semantics(ids)
    column_sems = await store.current_column_semantics(ids)
    live_cols = await store.live_columns(ids)
    evidence = await ProfileStore(session).load_evidence(ds_id, snapshot, enabled=True)
    raw = settings.llm.locality == "local" or settings.profiling.allow_raw_to_remote
    code_max = settings.profiling.code_max_distinct

    objects: dict[str, IndexedObject] = {}
    for t in snapshot.tables:
        rec = tables.get((t.schema, t.name))
        tsem = table_sems.get(rec.id) if rec is not None else None
        if tsem is not None and tsem.status in _EXCLUDED_STATUSES:
            tsem = None
        ev = evidence.table_evidence(t.qualified_name)
        pk = next((c.columns for c in t.constraints if c.kind == "primary_key"), [])
        obj = IndexedObject(
            schema=t.schema, name=t.name, kind=t.kind, rows=t.estimated_rows,
            business_name=tsem.business_name if tsem else None,
            purpose=tsem.purpose if tsem else None, grain=tsem.grain if tsem else None,
            entity=tsem.entity if tsem else None, synonyms=list(tsem.synonyms) if tsem else [],
            score=tsem.calibrated_score if tsem else None,
            status=tsem.status if tsem else None, primary_key=list(pk))
        cols_by_name = live_cols.get(rec.id, {}) if rec is not None else {}
        for c in t.columns:
            crec = cols_by_name.get(c.name)
            sem = column_sems.get(crec.id) if crec is not None else None
            if sem is not None and (sem.band in _EXCLUDED_BANDS
                                    or sem.status in _EXCLUDED_STATUSES or sem.unused):
                sem = None  # R2: every non-abstain meaning — this one is not used
            summary = ev.columns.get(c.name) if ev is not None else None
            stype = (sem.semantic_type if sem and sem.semantic_type else None) or \
                detected_type(summary)
            stated = list((sem.value_domain or {}).get("values") or []) if sem else []
            values, note = _values(snapshot.dialect, c.data_type, c.type_definition, summary,
                                   stated, raw, code_max)
            personal = (stype in PERSONAL_TYPES) or (summary is not None and any(
                d.semantic_type in PERSONAL_TYPES and d.match_rate >= _PERSONAL_MIN_RATE
                for d in summary.detectors))
            obj.columns.append(IndexedColumn(
                name=c.name, data_type=c.data_type, ordinal=c.ordinal,
                business_name=sem.business_name if sem else None,
                purpose=sem.purpose if sem else None, role=sem.role if sem else None,
                entity=sem.entity if sem else None, unit=sem.unit if sem else None,
                synonyms=list(sem.synonyms) if sem else [], semantic_type=stype,
                score=sem.calibrated_score if sem else None, band=sem.band if sem else None,
                status=sem.status if sem else None, values=values, value_note=note,
                meanings=dict((sem.value_domain or {}).get("meanings") or {}) if sem else {},
                distinct=summary.distinct if summary is not None else None,
                range=_range(snapshot.dialect, c.data_type, c.type_definition, summary, stype),
                is_key=c.name in pk, personal=personal))
        objects[t.qualified_name] = obj
    from sqlalchemy import or_, select

    from gaia.db.models.semantics import GlossaryTerm

    terms = (await session.scalars(select(GlossaryTerm).where(
        or_(GlossaryTerm.data_source_id == ds_id, GlossaryTerm.data_source_id.is_(None)),
        GlossaryTerm.status == "approved").order_by(GlossaryTerm.term))).all()
    return RetrievalIndex(data_source, snapshot.dialect, snapshot.database, objects, catalog,
                          data_source_id=ds_id, snapshot_id=catalog.snapshot_id,
                          glossary=[(g.term, g.definition, list(g.synonyms)) for g in terms])


async def vector_search(session, index: RetrievalIndex, vector: list[float], model: str,
                        k: int = 40) -> list[VectorHit]:
    """Nearest current table and column meanings of the data source (cosine).

    Exact scan over the data source's current versions: a few thousand vectors at
    most, and an approximate index under a filter could return fewer than ``k``."""
    literal = "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
    rows = await session.execute(text("""
        SELECT t.schema_name, t.object_name, k.column_name,
               1 - (e.embedding <=> CAST(:q AS vector)) AS similarity
        FROM semantics.embedding e
        JOIN catalog.tables t ON t.id = e.table_id
        LEFT JOIN catalog.columns k ON k.id = e.column_id
        LEFT JOIN semantics.table_semantic ts ON ts.id = e.table_semantic_id
        LEFT JOIN semantics.column_semantic cs ON cs.id = e.column_semantic_id
        WHERE t.data_source_id = :ds AND e.model = :model
          AND ((e.subject_kind = 'table' AND ts.is_current
                AND ts.status NOT IN ('sme_rejected', 'stale'))
            OR (e.subject_kind = 'column' AND cs.is_current AND cs.band <> 'abstain'
                AND cs.status NOT IN ('sme_rejected', 'stale') AND NOT cs.unused))
        ORDER BY e.embedding <=> CAST(:q AS vector)
        LIMIT :k
    """), {"q": literal, "ds": index.data_source_id or uuid.UUID(int=0), "model": model, "k": k})
    return [VectorHit(f"{s}.{o}", c, float(sim)) for s, o, c, sim in rows]
