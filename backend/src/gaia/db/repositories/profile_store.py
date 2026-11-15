"""Persistence for profiling (Phase 3A) and the evidence loader for reasoning.

Invariants:

* **Current, not overwritten.** A new profile of a table clears ``is_current``
  on the previous one (kept for history); a partial unique index guarantees one
  current profile per table. Tests and module definitions work the same way.
* **Tests follow their epoch.** Re-profiling a table replaces its Stage 1 join
  candidates; Stage 3 tests of claims survive re-profiling and are only
  replaced by a new test of the same claim, or dropped when the table's
  structure changed.
* **Modules are replaced only when their text changes** (``definition_hash``);
  view lineage is recomputed on every run (it also depends on the catalog).

``load_evidence`` turns the current profiles, tests and lineage into the
``TableEvidenceInput`` contract that reasoning consumes (3B). It reads the
metadata store only; the target is never touched.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import ColumnRecord, TableRecord
from gaia.db.models.profiling import (
    ColumnProfile,
    ModuleDefinition,
    ProfileRun,
    TableProfile,
    VerificationTest,
    ViewColumnLineage,
)
from gaia.introspect.snapshot import Snapshot
from gaia.profiling.summary import (
    ColumnProfileSummary,
    LineageInfo,
    StaticEvidence,
    TableEvidenceInput,
    VerificationResult,
    ViewAlias,
)

__all__ = ["ProfileStore", "summary_to_dict"]

# Skip reasons after which more data evidence cannot be had by profiling again.
FINAL_SKIPS = frozenset({"no_select", "catalog_sufficient", "view", "disabled"})


def summary_to_dict(summary: ColumnProfileSummary) -> dict[str, Any]:
    data = asdict(summary)
    data.pop("column", None)
    return data


def definition_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.strip().encode()).hexdigest()


class ProfileStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- runs ---------------------------------------------------------------------------------

    async def create_run(self, **fields: Any) -> ProfileRun:
        run = ProfileRun(**fields)
        self.session.add(run)
        await self.session.flush()
        return run

    # -- table profiles -----------------------------------------------------------------------

    async def current_profiles(
        self, table_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, TableProfile]:
        ids = list(table_ids)
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(TableProfile).where(
                TableProfile.table_id.in_(ids), TableProfile.is_current.is_(True)
            )
        )
        return {r.table_id: r for r in rows}

    async def write_table_profile(
        self,
        *,
        data_source_id: uuid.UUID,
        table_id: uuid.UUID,
        run_id: uuid.UUID | None,
        structure_hash: str,
        status: str,
        columns: dict[str, tuple[uuid.UUID | None, ColumnProfileSummary, dict[str, Any]]],
        structure_changed: bool = False,
        **fields: Any,
    ) -> TableProfile:
        """Insert the new current profile of a table; the previous one is kept as history.

        ``columns``: name -> (catalog column id, summary, per-metric provenance).
        """
        await self.session.execute(
            update(TableProfile)
            .where(TableProfile.table_id == table_id, TableProfile.is_current.is_(True))
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )
        # A new epoch: Stage 1 candidates are replaced; claim tests survive
        # unless the structure changed.
        stale = VerificationTest.origin == "candidate"
        if structure_changed:
            stale = VerificationTest.id.is_not(None)
        await self.session.execute(
            update(VerificationTest)
            .where(
                VerificationTest.table_id == table_id, VerificationTest.is_current.is_(True), stale
            )
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )
        profile = TableProfile(
            data_source_id=data_source_id,
            table_id=table_id,
            run_id=run_id,
            is_current=True,
            structure_hash=structure_hash,
            status=status,
            **fields,
        )
        self.session.add(profile)
        await self.session.flush()
        for name, (column_id, summary, metrics) in columns.items():
            self.session.add(
                ColumnProfile(
                    table_profile_id=profile.id,
                    table_id=table_id,
                    column_id=column_id,
                    column_name=name,
                    summary=summary_to_dict(summary),
                    metrics=metrics,
                )
            )
        await self.session.flush()
        return profile

    async def mark_stage3(self, table_ids: Iterable[uuid.UUID]) -> None:
        ids = list(table_ids)
        if ids:
            await self.session.execute(
                update(TableProfile)
                .where(TableProfile.table_id.in_(ids), TableProfile.is_current.is_(True))
                .values(stage3_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )

    # -- tests --------------------------------------------------------------------------------

    async def write_test(
        self,
        *,
        data_source_id: uuid.UUID,
        table_id: uuid.UUID,
        column_id: uuid.UUID | None,
        column_name: str,
        result: VerificationResult,
        stage: int,
        target: str | None,
        method: str,
        runtime_ms: int,
    ) -> VerificationTest:
        await self.session.execute(
            update(VerificationTest)
            .where(
                VerificationTest.table_id == table_id,
                VerificationTest.column_name == column_name,
                VerificationTest.test_id == result.test_id,
                VerificationTest.is_current.is_(True),
            )
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )
        row = VerificationTest(
            data_source_id=data_source_id,
            table_id=table_id,
            column_id=column_id,
            column_name=column_name,
            test_id=result.test_id,
            kind=result.kind,
            target=target,
            stage=stage,
            origin=result.origin,
            specific=result.specific,
            passed=result.passed,
            metric=result.metric,
            detail=result.detail,
            method=method,
            runtime_ms=runtime_ms,
            is_current=True,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def adopt_test(self, test: VerificationTest) -> VerificationTest:
        """A Stage 1 candidate test of a claimed join becomes a test of the claim."""
        result = VerificationResult(
            test_id=test.test_id,
            kind=test.kind,
            passed=test.passed,
            metric=test.metric,
            detail=(test.detail or "") + "; adopted as a test of the claim",
            origin="claim",
            specific=test.specific,
        )
        return await self.write_test(
            data_source_id=test.data_source_id,
            table_id=test.table_id,
            column_id=test.column_id,
            column_name=test.column_name,
            result=result,
            stage=3,
            target=test.target,
            method=test.method or "sampled_values",
            runtime_ms=0,
        )

    async def current_tests(
        self, table_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[VerificationTest]]:
        ids = list(table_ids)
        out: dict[uuid.UUID, list[VerificationTest]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = await self.session.scalars(
            select(VerificationTest).where(
                VerificationTest.table_id.in_(ids), VerificationTest.is_current.is_(True)
            )
        )
        for row in rows:
            out[row.table_id].append(row)
        return out

    # -- modules and lineage ------------------------------------------------------------------

    async def sync_modules(
        self, data_source_id: uuid.UUID, modules: list[tuple[str, str, str, str | None]]
    ) -> tuple[dict[tuple[str, str], ModuleDefinition], dict[str, int]]:
        """Store module definitions; replace only those whose text changed."""
        current = {
            (m.schema_name, m.object_name): m
            for m in await self.session.scalars(
                select(ModuleDefinition).where(
                    ModuleDefinition.data_source_id == data_source_id,
                    ModuleDefinition.is_current.is_(True),
                )
            )
        }
        counts = {"new": 0, "changed": 0, "unchanged": 0, "gone": 0}
        out: dict[tuple[str, str], ModuleDefinition] = {}
        seen: set[tuple[str, str]] = set()
        for schema, name, kind, text in modules:
            key = (schema, name)
            seen.add(key)
            digest = definition_hash(text)
            old = current.get(key)
            if old is not None and old.definition_hash == digest and old.kind == kind:
                counts["unchanged"] += 1
                out[key] = old
                continue
            if old is not None:
                old.is_current = False
                counts["changed"] += 1
            else:
                counts["new"] += 1
            await self.session.flush()
            row = ModuleDefinition(
                data_source_id=data_source_id,
                schema_name=schema,
                object_name=name,
                kind=kind,
                definition=text,
                definition_hash=digest,
                is_current=True,
            )
            self.session.add(row)
            out[key] = row
        for key, old in current.items():
            if key not in seen:
                old.is_current = False
                counts["gone"] += 1
        await self.session.flush()
        return out, counts

    async def replace_lineage(
        self, data_source_id: uuid.UUID, module: ModuleDefinition, lineage: list[Any]
    ) -> None:
        await self.session.execute(
            delete(ViewColumnLineage).where(
                ViewColumnLineage.data_source_id == data_source_id,
                ViewColumnLineage.view_schema == module.schema_name,
                ViewColumnLineage.view_name == module.object_name,
            )
        )
        for item in lineage:
            self.session.add(
                ViewColumnLineage(
                    module_definition_id=module.id,
                    data_source_id=data_source_id,
                    view_schema=module.schema_name,
                    view_name=module.object_name,
                    column_name=item.column,
                    classification=item.classification,
                    source=item.source,
                    expression=item.expression,
                    inputs=list(item.inputs),
                    converts=item.converts,
                    detail=item.detail or None,
                )
            )
        await self.session.flush()

    async def lineage(self, data_source_id: uuid.UUID) -> list[ViewColumnLineage]:
        rows = await self.session.scalars(
            select(ViewColumnLineage)
            .join(ModuleDefinition, ModuleDefinition.id == ViewColumnLineage.module_definition_id)
            .where(
                ViewColumnLineage.data_source_id == data_source_id,
                ModuleDefinition.is_current.is_(True),
            )
        )
        return list(rows)

    # -- evidence for reasoning ---------------------------------------------------------------

    async def load_evidence(
        self, data_source_id: uuid.UUID, snapshot: Snapshot, *, enabled: bool
    ) -> StaticEvidence:
        """The evidence contract for every live table of a data source.

        Per table, ``profiling_available`` says whether a later profiling run
        could still add data evidence: False after a final skip (no SELECT,
        catalog-sufficient, views) or when profiling is disabled; True when no
        profile exists yet or the last attempt ran out of budget, window or time.
        """
        tables = {
            (t.schema_name, t.object_name): t
            for t in await self.session.scalars(
                select(TableRecord).where(
                    TableRecord.data_source_id == data_source_id,
                    TableRecord.is_tombstoned.is_(False),
                )
            )
        }
        by_id = {t.id: t for t in tables.values()}
        profiles = await self.current_profiles(by_id)
        columns: dict[uuid.UUID, list[ColumnProfile]] = {}
        if profiles:
            rows = await self.session.scalars(
                select(ColumnProfile).where(
                    ColumnProfile.table_profile_id.in_([p.id for p in profiles.values()])
                )
            )
            for row in rows:
                columns.setdefault(row.table_id, []).append(row)
        tests = await self.current_tests(by_id)
        lineage = await self.lineage(data_source_id)

        evidence: dict[str, TableEvidenceInput] = {}
        kinds = {t.qualified_name: t.kind for t in snapshot.tables}

        def entry(schema: str, name: str) -> TableEvidenceInput:
            key = f"{schema}.{name}"
            if key not in evidence:
                record = tables.get((schema, name))
                profile = profiles.get(record.id) if record is not None else None
                available = enabled and kinds.get(key) == "table"
                if (
                    profile is not None
                    and profile.status == "skipped"
                    and (profile.skip_reason in FINAL_SKIPS)
                ):
                    available = False
                evidence[key] = TableEvidenceInput(table=key, profiling_available=available)
            return evidence[key]

        for (schema, name), record in tables.items():
            ev = entry(schema, name)
            profile = profiles.get(record.id)
            if profile is not None and profile.status in ("profiled", "partial"):
                data = {
                    "table": ev.table,
                    "columns": {c.column_name: c.summary for c in columns.get(record.id, [])},
                    "identities": (profile.evidence or {}).get("identities") or [],
                }
                loaded = TableEvidenceInput.from_dict(data)
                ev.columns, ev.identities = loaded.columns, loaded.identities
            for test in tests.get(record.id, []):
                ev.tests.setdefault(test.column_name, []).append(
                    VerificationResult(
                        test_id=test.test_id,
                        kind=test.kind,
                        passed=test.passed,
                        metric=test.metric,
                        detail=test.detail or "",
                        origin=test.origin,
                        specific=test.specific,
                        conclusive=test.method != "not_run",
                    )
                )

        for item in lineage:
            view = entry(item.view_schema, item.view_name)
            view.lineage[item.column_name] = LineageInfo(
                classification=item.classification,
                source=item.source,
                expression=item.expression,
                inputs=tuple(item.inputs or ()),
                detail=item.detail or "",
            )
            source = item.source if item.classification == "pass_through" else item.converts
            if not source:
                continue
            schema, table, column = source.rsplit(".", 2)
            if (schema, table) not in tables or item.column_name.lower() == column.lower():
                continue  # renamed columns only: the same name adds nothing
            base = entry(schema, table)
            base.aliases.setdefault(column, []).append(
                ViewAlias(
                    view=f"{item.view_schema}.{item.view_name}",
                    alias=item.column_name,
                    expression=item.expression if item.classification == "computed" else None,
                )
            )
        return StaticEvidence(evidence, profiling_available=enabled)

    # -- catalog helpers ----------------------------------------------------------------------

    async def column_ids(
        self, table_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, uuid.UUID]]:
        ids = list(table_ids)
        out: dict[uuid.UUID, dict[str, uuid.UUID]] = {i: {} for i in ids}
        if not ids:
            return out
        rows = await self.session.execute(
            select(ColumnRecord.table_id, ColumnRecord.column_name, ColumnRecord.id).where(
                ColumnRecord.table_id.in_(ids), ColumnRecord.is_tombstoned.is_(False)
            )
        )
        for table_id, name, column_id in rows:
            out[table_id][name] = column_id
        return out
