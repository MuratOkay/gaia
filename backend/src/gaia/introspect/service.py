"""Introspection runner: read a snapshot, diff it, persist it.

Kept separate from the reader so the pipeline order is explicit and testable:

    read -> diff against the previous snapshot -> apply (upsert + tombstone)

The catalog is **always** re-read in full. That is deliberate: a catalog pass is
one set of ``sys.*`` queries and costs seconds, while the expensive stages are
LLM reasoning and deep profiling. Skipping the read would risk missing a change
that the cheap signals do not expose (notably comment edits, which do not bump
``modify_date``). Cheapness is recovered downstream by hashing, which lets
unchanged tables skip reasoning entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings
from gaia.db.repositories.snapshot_store import ApplyResult, SnapshotStore
from gaia.dialects import bootstrap  # noqa: F401  (registers built-in dialects)
from gaia.dialects.registry import registry
from gaia.introspect.diff import DiffResult, diff_snapshots
from gaia.introspect.snapshot import Snapshot

logger = logging.getLogger(__name__)

__all__ = ["IntrospectionRun", "snapshot_from_document", "run_introspection"]


@dataclass
class IntrospectionRun:
    snapshot: Snapshot
    diff: DiffResult
    result: ApplyResult
    is_first_capture: bool


def snapshot_from_document(document: dict) -> Snapshot:
    """Rehydrate a stored snapshot document for diffing.

    Hashes are recomputed from the same canonical payloads rather than stored
    separately, so this cannot drift from the live reader.
    """
    from gaia.introspect.snapshot import (
        ColumnSnapshot,
        ConstraintSnapshot,
        IndexSnapshot,
        RelationSnapshot,
        TableSnapshot,
    )

    tables: list[TableSnapshot] = []
    for t in document.get("tables", []):
        stats_updated = t.get("stats_updated_at")
        table = TableSnapshot(
            schema=t["schema"],
            name=t["name"],
            kind=t["kind"],
            comment=t.get("comment"),
            estimated_rows=t.get("estimated_rows"),
            size_bytes=t.get("size_bytes"),
            # These two feed the data hash. Omitting them here made every
            # table look data-changed on the second capture, because the
            # rehydrated payload had None where the live read had real values.
            modification_counter=t.get("modification_counter"),
            stats_updated_at=(
                datetime.fromisoformat(stats_updated) if stats_updated else None
            ),
            partition_count=t.get("partition_count", 1),
        )
        for c in t.get("columns", []):
            table.columns.append(
                ColumnSnapshot(
                    name=c["name"],
                    ordinal=c.get("ordinal", 0),
                    data_type=c["data_type"],
                    nullable=c["nullable"],
                    default=c.get("default"),
                    comment=c.get("comment"),
                    is_identity=c.get("is_identity", False),
                    is_computed=c.get("is_computed", False),
                    is_sparse=c.get("is_sparse", False),
                    is_rowguidcol=c.get("is_rowguidcol", False),
                    max_length=c.get("max_length"),
                    precision=c.get("precision"),
                    scale=c.get("scale"),
                    collation=c.get("collation"),
                    is_user_defined_type=c.get("is_user_defined_type", False),
                    computed_definition=c.get("computed_definition"),
                    type_definition=c.get("type_definition"),
                )
            )
        for con in t.get("constraints", []):
            table.constraints.append(
                ConstraintSnapshot(
                    name=con["name"],
                    kind=con["kind"],
                    columns=con.get("columns", []),
                    definition=con.get("definition"),
                    is_disabled=con.get("is_disabled", False),
                    is_not_trusted=con.get("is_not_trusted", False),
                )
            )
        for r in t.get("relations", []):
            table.relations.append(
                RelationSnapshot(
                    name=r["name"],
                    from_schema=r["from_schema"],
                    from_table=r["from_table"],
                    from_columns=r.get("from_columns", []),
                    to_schema=r["to_schema"],
                    to_table=r["to_table"],
                    to_columns=r.get("to_columns", []),
                    on_delete=r.get("on_delete"),
                    on_update=r.get("on_update"),
                    is_disabled=r.get("is_disabled", False),
                    is_not_trusted=r.get("is_not_trusted", False),
                )
            )
        for i in t.get("indexes", []):
            table.indexes.append(
                IndexSnapshot(
                    name=i["name"],
                    is_primary_key=i.get("is_primary_key", False),
                    is_unique=i.get("is_unique", False),
                    is_unique_constraint=i.get("is_unique_constraint", False),
                    type_desc=i.get("type_desc", "CLUSTERED"),
                    columns=i.get("columns", []),
                    filter_definition=i.get("filter_definition"),
                    is_disabled=i.get("is_disabled", False),
                )
            )
        tables.append(table)

    return Snapshot(
        data_source=document.get("data_source", "unknown"),
        dialect=document.get("dialect", "unknown"),
        database=document.get("database", "unknown"),
        server_version=document.get("server_version") or "unknown",
        tables=tables,
    )



async def run_introspection(
    settings: Settings,
    session: AsyncSession,
    *,
    target_name: str | None = None,
    dry_run: bool = False,
) -> IntrospectionRun:
    """Read, diff and (unless dry-run) persist a snapshot."""
    target = settings.target(target_name)
    label = target_name or settings.app.default_target
    store = SnapshotStore(session)

    reader = registry.reader(target.dialect, config=target)
    snapshot = reader.read_snapshot(data_source=label)

    data_source = await store.ensure_data_source(snapshot, label)
    previous_record = await store.latest_snapshot(data_source.id)
    previous = (
        snapshot_from_document(previous_record.document)
        if previous_record is not None
        else None
    )

    diff = diff_snapshots(previous, snapshot)

    if dry_run:
        result = ApplyResult(
            snapshot_id="(dry-run)",
            data_source_id=str(data_source.id),
            diff=diff,
        )
    else:
        result = await store.apply(snapshot, data_source_name=label, diff=diff)

    return IntrospectionRun(
        snapshot=snapshot,
        diff=diff,
        result=result,
        is_first_capture=previous is None,
    )

