"""Additions for PostgreSQL must not move a single SQL Server hash or prompt.

The golden values were computed with the Phase 2 code (git HEAD before the
PostgreSQL dialect was added) on the shared sample snapshot. A change here
means every stored SQL Server table would re-reason on the next run.
"""

from __future__ import annotations

import hashlib

from gaia.introspect.snapshot import ColumnSnapshot
from gaia.semantics.bundle import build_bundle

PHASE2_SAMPLE_STRUCTURE_HASH = "6a8ef6a31ae1ef7f8bed4049c7b8419a44e0649ef6de5644bc60b521d2dfe37f"
PHASE2_SAMPLE_BUNDLES_SHA = "2b84c2face7efcaed8a65d7b4078c8b8b9a906cc11af088db2aa1d9fb01a0f0a"


def test_sample_snapshot_hash_unchanged(sample_snapshot):
    assert sample_snapshot.structure_hash == PHASE2_SAMPLE_STRUCTURE_HASH


def test_sample_bundles_render_unchanged(sample_snapshot):
    rendered = "".join(build_bundle(t).render() for t in sample_snapshot.tables)
    assert hashlib.sha256(rendered.encode()).hexdigest() == PHASE2_SAMPLE_BUNDLES_SHA


def test_type_definition_only_in_payload_when_set():
    plain = ColumnSnapshot(name="c", ordinal=1, data_type="int", nullable=False)
    assert "type_definition" not in plain.structure_payload()
    assert "type_definition" not in plain.to_dict()
    enum = ColumnSnapshot(
        name="c", ordinal=1, data_type="s.t", nullable=False, type_definition="enum ('a')"
    )
    assert enum.structure_payload()["type_definition"] == "enum ('a')"
    assert enum.structure_hash != plain.structure_hash


def test_type_definition_round_trips_through_the_document(sample_snapshot):
    from gaia.introspect.service import snapshot_from_document

    sample_snapshot.tables[0].columns[0].type_definition = "enum ('x', 'y')"
    again = snapshot_from_document(sample_snapshot.to_dict())
    assert again.tables[0].columns[0].type_definition == "enum ('x', 'y')"
    assert again.structure_hash == sample_snapshot.structure_hash
