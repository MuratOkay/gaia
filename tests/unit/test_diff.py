"""Diff semantics: what changes, and what each change invalidates."""

from __future__ import annotations

from gaia.introspect.diff import ChangeKind, diff_snapshots
from gaia.introspect.snapshot import ColumnSnapshot, Snapshot, TableSnapshot


def _snapshot(*tables: TableSnapshot) -> Snapshot:
    return Snapshot(
        data_source="t",
        dialect="mssql",
        database="db",
        server_version="17.0",
        tables=list(tables),
    )


def _table(name: str = "orders", **overrides) -> TableSnapshot:
    base = {
        "schema": "dbo",
        "name": name,
        "kind": "table",
        "estimated_rows": 100,
        "columns": [
            ColumnSnapshot(
                name="id", ordinal=1, data_type="bigint", nullable=False
            ),
            ColumnSnapshot(
                name="total", ordinal=2, data_type="decimal(14,6)", nullable=True
            ),
        ],
    }
    base.update(overrides)
    return TableSnapshot(**base)


# -- first capture ----------------------------------------------------------


def test_first_capture_marks_everything_added():
    result = diff_snapshots(None, _snapshot(_table("a"), _table("b")))

    assert len(result.changes) == 2
    assert all(c.kind is ChangeKind.ADDED for c in result.changes)
    assert all(c.invalidates_semantics for c in result.changes)
    assert result.unchanged == []


# -- no change --------------------------------------------------------------


def test_identical_snapshots_report_no_changes():
    a = _snapshot(_table())
    b = _snapshot(_table())

    result = diff_snapshots(a, b)

    assert result.changes == []
    assert result.unchanged == ["dbo.orders"]
    assert result.summary()["needs_reasoning"] == 0


def test_column_reordering_is_not_a_change():
    reordered = _table(
        columns=[
            ColumnSnapshot(name="total", ordinal=1, data_type="decimal(14,6)", nullable=True),
            ColumnSnapshot(name="id", ordinal=2, data_type="bigint", nullable=False),
        ]
    )

    result = diff_snapshots(_snapshot(_table()), _snapshot(reordered))

    assert result.changes == []
    assert result.unchanged == ["dbo.orders"]


# -- structural change ------------------------------------------------------


def test_comment_change_is_structural_and_detected():
    """The case modify_date cannot see: sp_addextendedproperty does not bump it."""
    commented = _table(comment="Customer orders")

    result = diff_snapshots(_snapshot(_table()), _snapshot(commented))

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind is ChangeKind.STRUCTURE_CHANGED
    assert change.invalidates_semantics is True
    # A table-level comment is not a column change.
    assert change.added_columns == []
    assert change.changed_columns == []
    assert change.removed_columns == []


def test_added_column_is_localized():
    extended = _table()
    extended.columns.append(
        ColumnSnapshot(name="note", ordinal=3, data_type="nvarchar(200)", nullable=True)
    )

    result = diff_snapshots(_snapshot(_table()), _snapshot(extended))

    change = result.changes[0]
    assert change.kind is ChangeKind.STRUCTURE_CHANGED
    assert change.added_columns == ["note"]
    assert change.changed_columns == []
    assert change.removed_columns == []


def test_changed_column_is_localized():
    retyped = _table()
    retyped.columns[1] = ColumnSnapshot(
        name="total", ordinal=2, data_type="decimal(18,6)", nullable=True
    )

    result = diff_snapshots(_snapshot(_table()), _snapshot(retyped))

    change = result.changes[0]
    assert change.changed_columns == ["total"]
    assert change.added_columns == []
    assert change.removed_columns == []


def test_removed_column_is_localized():
    reduced = _table()
    reduced.columns = [reduced.columns[0]]

    result = diff_snapshots(_snapshot(_table()), _snapshot(reduced))

    assert result.changes[0].removed_columns == ["total"]


# -- data-only change -------------------------------------------------------


def test_row_count_change_is_data_only():
    grown = _table(estimated_rows=500, modification_counter=42)

    result = diff_snapshots(_snapshot(_table()), _snapshot(grown))

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind is ChangeKind.DATA_CHANGED
    # Data moving must NOT force re-reasoning about meaning.
    assert change.invalidates_semantics is False
    assert result.needs_reasoning == []


# -- add / remove tables ----------------------------------------------------


def test_added_table():
    result = diff_snapshots(_snapshot(_table("a")), _snapshot(_table("a"), _table("b")))

    added = result.added
    assert len(added) == 1
    assert added[0].qualified_name == "dbo.b"
    assert added[0].added_columns == ["id", "total"]


def test_removed_table():
    result = diff_snapshots(_snapshot(_table("a"), _table("b")), _snapshot(_table("a")))

    removed = result.removed
    assert len(removed) == 1
    assert removed[0].qualified_name == "dbo.b"
    assert removed[0].invalidates_semantics is True


def test_summary_counts():
    result = diff_snapshots(
        _snapshot(_table("keep"), _table("gone")),
        _snapshot(_table("keep"), _table("new")),
    )

    summary = result.summary()
    assert summary["added"] == 1
    assert summary["removed"] == 1
    assert summary["unchanged"] == 1
