"""Structural diff between two snapshots.

Answers a narrower question than a generic diff: *what actually changed, and
what does that invalidate?* The expensive stages downstream are LLM reasoning
and deep profiling, so the diff must be precise enough to skip unchanged work
entirely, and honest enough never to skip something that did change.

Comparison is hash-based, not value-based:

* ``old.structure_hash == new.structure_hash`` → nothing to do.
* Otherwise individual sub-hashes localize the change to a column, constraint,
  relation or index, so reasoning can be scoped rather than re-running a whole
  table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gaia.introspect.snapshot import Snapshot, TableSnapshot


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    STRUCTURE_CHANGED = "structure_changed"
    DATA_CHANGED = "data_changed"


@dataclass
class ObjectChange:
    """A change to a single table or view."""

    schema: str
    name: str
    kind: ChangeKind
    old_structure_hash: str | None = None
    new_structure_hash: str | None = None
    old_data_hash: str | None = None
    new_data_hash: str | None = None
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    changed_columns: list[str] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def invalidates_semantics(self) -> bool:
        """Structure changes invalidate reasoning; data changes do not."""
        return self.kind in (
            ChangeKind.ADDED,
            ChangeKind.REMOVED,
            ChangeKind.STRUCTURE_CHANGED,
        )

    @property
    def invalidates_profiles(self) -> bool:
        return self.kind is not ChangeKind.STRUCTURE_CHANGED or bool(
            self.added_columns or self.removed_columns or self.changed_columns
        )

    def summary(self) -> str:
        parts = [f"{self.qualified_name}: {self.kind.value}"]
        if self.added_columns:
            parts.append(f"+{len(self.added_columns)} columns")
        if self.removed_columns:
            parts.append(f"-{len(self.removed_columns)} columns")
        if self.changed_columns:
            parts.append(f"~{len(self.changed_columns)} columns")
        return ", ".join(parts)


@dataclass
class DiffResult:
    changes: list[ObjectChange] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    structure_hash_changed: bool = False
    data_hash_changed: bool = False

    def by_kind(self, kind: ChangeKind) -> list[ObjectChange]:
        return [c for c in self.changes if c.kind is kind]

    @property
    def added(self) -> list[ObjectChange]:
        return self.by_kind(ChangeKind.ADDED)

    @property
    def removed(self) -> list[ObjectChange]:
        return self.by_kind(ChangeKind.REMOVED)

    @property
    def structure_changed(self) -> list[ObjectChange]:
        return self.by_kind(ChangeKind.STRUCTURE_CHANGED)

    @property
    def data_changed(self) -> list[ObjectChange]:
        return self.by_kind(ChangeKind.DATA_CHANGED)

    @property
    def needs_reasoning(self) -> list[ObjectChange]:
        return [c for c in self.changes if c.invalidates_semantics]

    def summary(self) -> dict[str, Any]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "structure_changed": len(self.structure_changed),
            "data_changed": len(self.data_changed),
            "unchanged": len(self.unchanged),
            "needs_reasoning": len(self.needs_reasoning),
        }


def _column_hashes(table: TableSnapshot) -> dict[str, str]:
    return {c.name: c.structure_hash for c in table.columns}


def _compare_table(old: TableSnapshot, new: TableSnapshot) -> ObjectChange:
    """Localize a structural change to the column level where possible."""
    old_cols = _column_hashes(old)
    new_cols = _column_hashes(new)

    added = sorted(set(new_cols) - set(old_cols))
    removed = sorted(set(old_cols) - set(new_cols))
    changed = sorted(
        name for name in set(old_cols) & set(new_cols) if old_cols[name] != new_cols[name]
    )

    return ObjectChange(
        schema=new.schema,
        name=new.name,
        kind=ChangeKind.STRUCTURE_CHANGED,
        old_structure_hash=old.structure_hash,
        new_structure_hash=new.structure_hash,
        old_data_hash=old.data_hash,
        new_data_hash=new.data_hash,
        added_columns=added,
        removed_columns=removed,
        changed_columns=changed,
    )



def diff_snapshots(old: Snapshot | None, new: Snapshot) -> DiffResult:
    """Compare a previous snapshot against a fresh one.

    ``old is None`` means a first capture: every object is reported as added,
    so the caller reasons about everything exactly once.
    """
    result = DiffResult()

    if old is None:
        for table in new.tables:
            result.changes.append(
                ObjectChange(
                    schema=table.schema,
                    name=table.name,
                    kind=ChangeKind.ADDED,
                    new_structure_hash=table.structure_hash,
                    new_data_hash=table.data_hash,
                    added_columns=[c.name for c in table.columns],
                )
            )
        result.structure_hash_changed = True
        result.data_hash_changed = True
        return result

    result.structure_hash_changed = old.structure_hash != new.structure_hash
    result.data_hash_changed = old.data_hash != new.data_hash

    old_tables = {(t.schema, t.name): t for t in old.tables}
    new_tables = {(t.schema, t.name): t for t in new.tables}

    for key in sorted(new_tables):
        new_table = new_tables[key]
        old_table = old_tables.get(key)

        if old_table is None:
            result.changes.append(
                ObjectChange(
                    schema=new_table.schema,
                    name=new_table.name,
                    kind=ChangeKind.ADDED,
                    new_structure_hash=new_table.structure_hash,
                    new_data_hash=new_table.data_hash,
                    added_columns=[c.name for c in new_table.columns],
                )
            )
            continue

        if old_table.structure_hash != new_table.structure_hash:
            result.changes.append(_compare_table(old_table, new_table))
        elif old_table.data_hash != new_table.data_hash:
            result.changes.append(
                ObjectChange(
                    schema=new_table.schema,
                    name=new_table.name,
                    kind=ChangeKind.DATA_CHANGED,
                    old_structure_hash=old_table.structure_hash,
                    new_structure_hash=new_table.structure_hash,
                    old_data_hash=old_table.data_hash,
                    new_data_hash=new_table.data_hash,
                )
            )
        else:
            result.unchanged.append(new_table.qualified_name)

    for key in sorted(set(old_tables) - set(new_tables)):
        old_table = old_tables[key]
        result.changes.append(
            ObjectChange(
                schema=old_table.schema,
                name=old_table.name,
                kind=ChangeKind.REMOVED,
                old_structure_hash=old_table.structure_hash,
                old_data_hash=old_table.data_hash,
                removed_columns=[c.name for c in old_table.columns],
            )
        )

    return result

