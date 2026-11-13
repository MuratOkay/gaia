"""Snapshot document round-trip.

Regression guard: ``snapshot_from_document`` originally dropped
``modification_counter`` and ``stats_updated_at``, which made every table look
data-changed on the second capture. Rehydration must reproduce all hash inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gaia.introspect.service import snapshot_from_document
from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    IndexSnapshot,
    RelationSnapshot,
    Snapshot,
    TableSnapshot,
)


def _full_snapshot() -> Snapshot:
    table = TableSnapshot(
        schema="dbo",
        name="transactions",
        kind="table",
        comment="Business transaction header",
        estimated_rows=12345,
        size_bytes=987654,
        modification_counter=77,
        stats_updated_at=datetime(2026, 11, 12, 9, 30, tzinfo=UTC),
        partition_count=1,
        columns=[
            ColumnSnapshot(
                name="transaction_id",
                ordinal=1,
                data_type="bigint",
                nullable=False,
                is_identity=True,
                comment="Surrogate key",
            ),
            ColumnSnapshot(
                name="amount",
                ordinal=2,
                data_type="decimal(14,6)",
                nullable=True,
                default="((0))",
            ),
        ],
        constraints=[
            ConstraintSnapshot(
                name="pk_transactions", kind="primary_key", columns=["transaction_id"]
            )
        ],
        relations=[
            RelationSnapshot(
                name="fk_tx_customer",
                from_schema="dbo",
                from_table="transactions",
                from_columns=["customer_id"],
                to_schema="dbo",
                to_table="customers",
                to_columns=["customer_id"],
                on_delete="NO_ACTION",
            )
        ],
        indexes=[
            IndexSnapshot(
                name="ix_tx_customer",
                is_primary_key=False,
                is_unique=False,
                is_unique_constraint=False,
                type_desc="NONCLUSTERED",
                columns=[{"name": "customer_id", "key_ordinal": 1}],
            )
        ],
    )
    return Snapshot(
        data_source="demo",
        dialect="mssql",
        database="nge_small",
        server_version="17.0.4065.4",
        tables=[table],
    )


def test_round_trip_preserves_structure_hash():
    original = _full_snapshot()

    restored = snapshot_from_document(original.to_dict())

    assert restored.structure_hash == original.structure_hash


def test_round_trip_preserves_data_hash():
    """The bug this guards: data-hash inputs must survive serialization."""
    original = _full_snapshot()

    restored = snapshot_from_document(original.to_dict())

    assert restored.data_hash == original.data_hash


def test_round_trip_preserves_table_identity():
    original = _full_snapshot()

    restored = snapshot_from_document(original.to_dict())

    table = restored.table("dbo", "transactions")
    assert table is not None
    assert table.kind == "table"
    assert table.estimated_rows == 12345
    assert table.modification_counter == 77
    assert table.stats_updated_at == datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    assert [c.name for c in table.columns] == ["transaction_id", "amount"]


def test_round_trip_preserves_children():
    original = _full_snapshot()

    restored = snapshot_from_document(original.to_dict())
    table = restored.table("dbo", "transactions")

    assert table is not None
    assert len(table.constraints) == 1
    assert table.constraints[0].columns == ["transaction_id"]
    assert len(table.relations) == 1
    assert table.relations[0].to_table == "customers"
    assert len(table.indexes) == 1
    assert table.indexes[0].columns[0]["name"] == "customer_id"


def test_round_trip_survives_null_stats():
    table = TableSnapshot(
        schema="dbo",
        name="v",
        kind="view",
        estimated_rows=None,
        stats_updated_at=None,
    )
    original = Snapshot(
        data_source="demo",
        dialect="mssql",
        database="db",
        server_version="17.0",
        tables=[table],
    )

    restored = snapshot_from_document(original.to_dict())

    assert restored.table("dbo", "v") is not None
    assert restored.data_hash == original.data_hash
