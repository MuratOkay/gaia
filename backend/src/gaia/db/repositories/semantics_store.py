"""Persistence for reasoning runs, versioned semantics, join hints and embeddings.

Invariants enforced here:

* **Versioned, never overwritten.** Writing a table's semantics inserts version
  N+1 for the table and each of its columns, after clearing ``is_current`` on
  version N. Partial unique indexes guarantee one current row per subject.
* **Stale is explicit.** When a table's structure changes, or a table or column
  is tombstoned, its current semantics are marked ``stale`` straight away.
  If re-reasoning then fails, nothing stale passes for current truth.
* **Declared FKs are confirmed join hints.** They are synced from the snapshot
  on every run; an FK that disappears is marked ``stale``, never deleted. Rows
  from the model (``llm``) never overwrite FK or SME rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.audit import LLMCall
from gaia.db.models.catalog import (
    ColumnRecord,
    DataSource,
    SnapshotRecord,
    TableRecord,
)
from gaia.db.models.semantics import (
    ColumnSemantic,
    Embedding,
    JoinHint,
    ReasoningRun,
    TableSemantic,
)
from gaia.introspect.snapshot import Snapshot
from gaia.llm.structured import CallRecord

__all__ = ["EmbeddingTarget", "SemanticsStore"]


@dataclass
class EmbeddingTarget:
    """A current semantic row that still needs a vector for ``model``."""

    subject_kind: str  # table | column
    semantic_id: uuid.UUID
    table_id: uuid.UUID
    column_id: uuid.UUID | None
    text: str


class SemanticsStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- catalog reads ---------------------------------------------------------

    async def data_source(self, name: str) -> DataSource | None:
        return await self.session.scalar(select(DataSource).where(DataSource.name == name))

    async def latest_snapshot(self, data_source_id: uuid.UUID) -> SnapshotRecord | None:
        return await self.session.scalar(
            select(SnapshotRecord)
            .where(SnapshotRecord.data_source_id == data_source_id)
            .order_by(SnapshotRecord.captured_at.desc())
            .limit(1)
        )

    async def live_tables(self, data_source_id: uuid.UUID) -> dict[tuple[str, str], TableRecord]:
        rows = await self.session.scalars(
            select(TableRecord).where(
                TableRecord.data_source_id == data_source_id,
                TableRecord.is_tombstoned.is_(False),
            )
        )
        return {(t.schema_name, t.object_name): t for t in rows}

    async def live_columns(
        self, table_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, ColumnRecord]]:
        ids = list(table_ids)
        out: dict[uuid.UUID, dict[str, ColumnRecord]] = {i: {} for i in ids}
        if not ids:
            return out
        rows = await self.session.scalars(
            select(ColumnRecord).where(
                ColumnRecord.table_id.in_(ids), ColumnRecord.is_tombstoned.is_(False)
            )
        )
        for col in rows:
            out[col.table_id][col.column_name] = col
        return out

    # -- current semantics -----------------------------------------------------

    async def current_table_semantics(
        self, table_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, TableSemantic]:
        ids = list(table_ids)
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(TableSemantic).where(
                TableSemantic.table_id.in_(ids), TableSemantic.is_current.is_(True)
            )
        )
        return {r.table_id: r for r in rows}

    async def mark_stale(self, data_source_id: uuid.UUID) -> dict[str, int]:
        """Flag current semantics whose catalog object changed or disappeared."""
        changed_tables = (
            select(TableRecord.id)
            .join(TableSemantic, TableSemantic.table_id == TableRecord.id)
            .where(
                TableRecord.data_source_id == data_source_id,
                TableSemantic.is_current.is_(True),
                or_(
                    TableRecord.is_tombstoned.is_(True),
                    TableSemantic.structure_hash != TableRecord.structure_hash,
                ),
            )
        )
        t = await self.session.execute(
            update(TableSemantic)
            .where(
                TableSemantic.table_id.in_(changed_tables),
                TableSemantic.is_current.is_(True),
                TableSemantic.status != "stale",
            )
            .values(status="stale")
            .execution_options(synchronize_session=False)
        )
        gone_columns = select(ColumnRecord.id).where(ColumnRecord.is_tombstoned.is_(True))
        c = await self.session.execute(
            update(ColumnSemantic)
            .where(
                ColumnSemantic.is_current.is_(True),
                ColumnSemantic.status != "stale",
                or_(
                    ColumnSemantic.table_id.in_(changed_tables),
                    ColumnSemantic.column_id.in_(gone_columns),
                ),
            )
            .values(status="stale")
            .execution_options(synchronize_session=False)
        )
        return {"tables": t.rowcount or 0, "columns": c.rowcount or 0}

    # -- runs and audit ----------------------------------------------------------

    async def create_run(self, **fields: Any) -> ReasoningRun:
        run = ReasoningRun(**fields)
        self.session.add(run)
        await self.session.flush()
        return run

    async def record_calls(
        self,
        calls: Sequence[CallRecord],
        *,
        run_id: uuid.UUID | None,
        data_source_id: uuid.UUID | None,
        table_id: uuid.UUID | None,
    ) -> None:
        for c in calls:
            self.session.add(
                LLMCall(
                    run_id=run_id,
                    data_source_id=data_source_id,
                    table_id=table_id,
                    purpose=c.purpose,
                    model=c.model,
                    prompt_version=c.prompt_version,
                    prompt_hash=c.prompt_hash,
                    thinking=c.thinking,
                    json_mode=c.json_mode,
                    attempt=c.attempt,
                    cache_hit=c.cache_hit,
                    parsed_ok=c.parsed_ok,
                    error=c.error,
                    finish_reason=c.finish_reason,
                    request_id=c.request_id,
                    prompt_tokens=c.usage.prompt_tokens,
                    completion_tokens=c.usage.completion_tokens,
                    reasoning_tokens=c.usage.reasoning_tokens,
                    cached_tokens=c.usage.cached_tokens,
                    total_tokens=c.usage.total_tokens,
                    latency_ms=c.latency_ms,
                    transport_attempts=c.transport_attempts,
                    raw_response=c.content,
                    reasoning=c.reasoning,
                )
            )
        await self.session.flush()

    # -- versioned writes ------------------------------------------------------------

    async def write_table_version(
        self,
        table_row: dict[str, Any],
        column_rows: list[dict[str, Any]],
    ) -> tuple[TableSemantic, list[ColumnSemantic]]:
        """Insert a new current version for a table and its columns.

        ``table_row``/``column_rows`` carry every field except ``version``,
        ``is_current`` and ``table_semantic_id``, which are assigned here.
        """
        table_id = table_row["table_id"]
        next_table_version = (
            await self.session.scalar(
                select(func.coalesce(func.max(TableSemantic.version), 0)).where(
                    TableSemantic.table_id == table_id
                )
            )
        ) + 1

        column_ids = [r["column_id"] for r in column_rows]
        column_versions: dict[uuid.UUID, int] = {}
        if column_ids:
            result = await self.session.execute(
                select(ColumnSemantic.column_id, func.max(ColumnSemantic.version))
                .where(ColumnSemantic.column_id.in_(column_ids))
                .group_by(ColumnSemantic.column_id)
            )
            column_versions = {cid: v for cid, v in result.all()}

        # Retire the previous current rows first: the partial unique indexes
        # allow only one current row per table and per column.
        await self.session.execute(
            update(TableSemantic)
            .where(TableSemantic.table_id == table_id, TableSemantic.is_current.is_(True))
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(
            update(ColumnSemantic)
            .where(
                or_(
                    ColumnSemantic.table_id == table_id,
                    ColumnSemantic.column_id.in_(column_ids),
                ),
                ColumnSemantic.is_current.is_(True),
            )
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )

        table_sem = TableSemantic(**table_row, version=next_table_version, is_current=True)
        self.session.add(table_sem)
        await self.session.flush()

        column_sems: list[ColumnSemantic] = []
        for row in column_rows:
            sem = ColumnSemantic(
                **row,
                table_semantic_id=table_sem.id,
                version=column_versions.get(row["column_id"], 0) + 1,
                is_current=True,
            )
            self.session.add(sem)
            column_sems.append(sem)
        await self.session.flush()
        return table_sem, column_sems

    # -- join hints ------------------------------------------------------------------

    async def sync_fk_join_hints(
        self, data_source_id: uuid.UUID, snapshot: Snapshot
    ) -> dict[str, int]:
        """Mirror declared foreign keys as confirmed join hints."""
        paths: set[tuple] = set()
        upserted = 0
        for table in snapshot.tables:
            for rel in table.relations:
                key = (
                    rel.from_schema,
                    rel.from_table,
                    tuple(rel.from_columns),
                    rel.to_schema,
                    rel.to_table,
                    tuple(rel.to_columns),
                )
                if key in paths:
                    continue
                paths.add(key)
                stmt = pg_insert(JoinHint).values(
                    id=uuid.uuid4(),
                    data_source_id=data_source_id,
                    from_schema=rel.from_schema,
                    from_table=rel.from_table,
                    from_columns=list(rel.from_columns),
                    to_schema=rel.to_schema,
                    to_table=rel.to_table,
                    to_columns=list(rel.to_columns),
                    source="fk",
                    status="confirmed",
                    relation_name=rel.name,
                    confidence=1.0,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_join_hint_path",
                    set_={
                        "source": "fk",
                        "status": "confirmed",
                        "relation_name": rel.name,
                        "confidence": 1.0,
                        "updated_at": func.now(),
                    },
                    # SME decisions are never overridden by automation.
                    where=JoinHint.source != "sme",
                )
                await self.session.execute(stmt)
                upserted += 1

        existing = await self.session.scalars(
            select(JoinHint).where(
                JoinHint.data_source_id == data_source_id,
                JoinHint.source == "fk",
                JoinHint.status != "stale",
            )
        )
        staled = 0
        for hint in existing:
            key = (
                hint.from_schema,
                hint.from_table,
                tuple(hint.from_columns),
                hint.to_schema,
                hint.to_table,
                tuple(hint.to_columns),
            )
            if key not in paths:
                hint.status = "stale"
                staled += 1
        await self.session.flush()
        return {"upserted": upserted, "staled": staled}

    async def upsert_inferred_join_hints(
        self,
        data_source_id: uuid.UUID,
        hints: Iterable[Any],
        *,
        run_id: uuid.UUID,
        prompt_version: str,
        model: str,
    ) -> int:
        written = 0
        for h in hints:
            stmt = pg_insert(JoinHint).values(
                id=uuid.uuid4(),
                data_source_id=data_source_id,
                from_schema=h.from_schema,
                from_table=h.from_table,
                from_columns=list(h.from_columns),
                to_schema=h.to_schema,
                to_table=h.to_table,
                to_columns=list(h.to_columns),
                source="llm",
                status="inferred",
                confidence=h.confidence,
                rationale=h.rationale,
                run_id=run_id,
                prompt_version=prompt_version,
                model=model,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_join_hint_path",
                set_={
                    "confidence": h.confidence,
                    "rationale": h.rationale,
                    "run_id": run_id,
                    "prompt_version": prompt_version,
                    "model": model,
                    "updated_at": func.now(),
                },
                # Only refresh the model's own rows; keep status (an SME may
                # have rejected it) and never touch FK or SME rows.
                where=JoinHint.source == "llm",
            )
            await self.session.execute(stmt)
            written += 1
        await self.session.flush()
        return written

    # -- embeddings ------------------------------------------------------------------

    async def embedding_targets(
        self, data_source_id: uuid.UUID, model: str, table_ids: Iterable[uuid.UUID] | None = None
    ) -> list[EmbeddingTarget]:
        """Current semantic rows without a vector for ``model``."""
        from gaia.semantics.embeddings import column_embedding_text, table_embedding_text

        ids = list(table_ids) if table_ids is not None else None
        has_table_vec = (
            select(Embedding.id)
            .where(Embedding.table_semantic_id == TableSemantic.id, Embedding.model == model)
            .exists()
        )
        tq = (
            select(TableSemantic, TableRecord)
            .join(TableRecord, TableRecord.id == TableSemantic.table_id)
            .where(
                TableSemantic.data_source_id == data_source_id,
                TableSemantic.is_current.is_(True),
                ~has_table_vec,
            )
        )
        if ids is not None:
            tq = tq.where(TableSemantic.table_id.in_(ids))

        targets: list[EmbeddingTarget] = []
        table_rows = (await self.session.execute(tq)).all()

        names_by_table: dict[uuid.UUID, list[str]] = {}
        if table_rows:
            result = await self.session.execute(
                select(ColumnSemantic.table_semantic_id, ColumnSemantic.business_name)
                .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
                .where(ColumnSemantic.table_semantic_id.in_([ts.id for ts, _ in table_rows]))
                .order_by(ColumnSemantic.table_semantic_id, ColumnRecord.ordinal)
            )
            for ts_id, name in result.all():
                names_by_table.setdefault(ts_id, []).append(name)

        for ts, tr in table_rows:
            targets.append(
                EmbeddingTarget(
                    subject_kind="table",
                    semantic_id=ts.id,
                    table_id=tr.id,
                    column_id=None,
                    text=table_embedding_text(
                        schema=tr.schema_name,
                        table=tr.object_name,
                        business_name=ts.business_name,
                        purpose=ts.purpose,
                        grain=ts.grain,
                        entity=ts.entity,
                        synonyms=list(ts.synonyms or []),
                        column_business_names=names_by_table.get(ts.id, []),
                    ),
                )
            )

        has_col_vec = (
            select(Embedding.id)
            .where(Embedding.column_semantic_id == ColumnSemantic.id, Embedding.model == model)
            .exists()
        )
        cq = (
            select(ColumnSemantic, ColumnRecord, TableRecord, TableSemantic)
            .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
            .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
            .join(TableSemantic, TableSemantic.id == ColumnSemantic.table_semantic_id)
            .where(
                TableRecord.data_source_id == data_source_id,
                ColumnSemantic.is_current.is_(True),
                ~has_col_vec,
            )
            .order_by(TableRecord.schema_name, TableRecord.object_name, ColumnRecord.ordinal)
        )
        if ids is not None:
            cq = cq.where(ColumnSemantic.table_id.in_(ids))
        for cs, cr, tr, ts in (await self.session.execute(cq)).all():
            targets.append(
                EmbeddingTarget(
                    subject_kind="column",
                    semantic_id=cs.id,
                    table_id=tr.id,
                    column_id=cr.id,
                    text=column_embedding_text(
                        schema=tr.schema_name,
                        table=tr.object_name,
                        column=cr.column_name,
                        business_name=cs.business_name,
                        purpose=cs.purpose,
                        role=cs.role,
                        entity=cs.entity,
                        unit=cs.unit,
                        synonyms=list(cs.synonyms or []),
                        table_business_name=ts.business_name,
                        table_purpose=ts.purpose,
                    ),
                )
            )
        return targets

    async def add_embeddings(
        self,
        targets: Sequence[EmbeddingTarget],
        vectors: Sequence[list[float]],
        *,
        model: str,
        dimensions: int,
    ) -> int:
        from gaia.semantics.embeddings import content_hash

        for target, vector in zip(targets, vectors, strict=True):
            self.session.add(
                Embedding(
                    subject_kind=target.subject_kind,
                    table_semantic_id=target.semantic_id
                    if target.subject_kind == "table"
                    else None,
                    column_semantic_id=(
                        target.semantic_id if target.subject_kind == "column" else None
                    ),
                    table_id=target.table_id,
                    column_id=target.column_id,
                    content=target.text,
                    content_hash=content_hash(target.text),
                    model=model,
                    dimensions=dimensions,
                    embedding=vector,
                )
            )
        await self.session.flush()
        return len(targets)

    # -- reporting -------------------------------------------------------------------

    async def current_column_scores(
        self, table_ids: Iterable[uuid.UUID]
    ) -> list[tuple[float, str]]:
        ids = list(table_ids)
        if not ids:
            return []
        result = await self.session.execute(
            select(ColumnSemantic.calibrated_score, ColumnSemantic.band).where(
                and_(ColumnSemantic.table_id.in_(ids), ColumnSemantic.is_current.is_(True))
            )
        )
        return [(float(s), b) for s, b in result.all()]
