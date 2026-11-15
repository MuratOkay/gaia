"""Wide-table chunking (D17', Phase 3B)."""

from __future__ import annotations

import json

from gaia.introspect.snapshot import ColumnSnapshot, ConstraintSnapshot, TableSnapshot
from gaia.semantics.bundle import build_bundle, build_catalog_index
from gaia.semantics.chunking import chunk_bundle, merge_critiques, merge_reasonings, plan_chunks
from gaia.semantics.evidence import Exposure, render_evidence
from gaia.semantics.reasoner import ReasonerSettings, TableReasoner


def _wide(n: int = 60) -> TableSnapshot:
    cols = [ColumnSnapshot(name="id", ordinal=1, data_type="int", nullable=False)]
    cols += [ColumnSnapshot(name=f"attr_{i:02d}", ordinal=i + 1, data_type="varchar(40)",
                            nullable=True) for i in range(1, n)]
    return TableSnapshot(schema="dw", name="sales_wide", kind="table", estimated_rows=10,
                         columns=cols,
                         constraints=[ConstraintSnapshot("PK_w", "primary_key", ["id"]),
                                      ConstraintSnapshot("CK_a", "check", ["attr_05"],
                                                         "([attr_05]<>'')")])


def test_no_split_when_it_fits():
    bundle = build_bundle(_wide(10))
    assert plan_chunks(bundle, None, fixed_tokens=1000, context_budget=32_000,
                       max_output_tokens=16_000) == [bundle.column_names]


def test_split_by_context_budget_keeps_order_and_covers_every_column():
    bundle = build_bundle(_wide(60))
    chunks = plan_chunks(bundle, None, fixed_tokens=1000, context_budget=2000,
                         max_output_tokens=None)
    assert 1 < len(chunks) < 10
    assert [c for chunk in chunks for c in chunk] == bundle.column_names


def test_split_by_output_budget():
    bundle = build_bundle(_wide(60))
    chunks = plan_chunks(bundle, None, fixed_tokens=100, context_budget=10**6,
                         max_output_tokens=4000)
    assert max(len(c) for c in chunks) <= 8  # (4000*0.8 - 400) // 350


def test_chunk_bundle_keeps_context():
    bundle = build_bundle(_wide(20))
    part = chunk_bundle(bundle, ["attr_05", "attr_06"], 2, 3)
    doc = part.document
    assert [c["name"] for c in doc["columns"]] == ["attr_05", "attr_06"]
    assert doc["chunk"]["part"] == 2 and doc["primary_key"]["columns"] == ["id"]
    assert [c["ref"] for c in doc["check_constraints"]] == ["ck:CK_a"]
    assert "id int" in doc["other_columns"]
    assert part.column_names == ["attr_05", "attr_06"]
    assert list(doc)[:3] == ["table", "kind", "chunk"]


def test_reasoner_chunks_a_wide_table(fake_llm):
    table = _wide(40)
    bundle = build_bundle(table)
    from gaia.introspect.snapshot import Snapshot

    index = build_catalog_index(Snapshot(data_source="x", dialect="mssql", database="d",
                                         server_version="1", tables=[table]))
    settings = ReasonerSettings(model="fake", thinking=False, critique_thinking=False,
                                max_tokens=5000, temperature=None, prompt_major=2,
                                context_budget_tokens=32_000)
    reasoner = TableReasoner(fake_llm, settings, index)
    evidence = render_evidence(bundle, None, Exposure("masked"))
    outcome = reasoner.run(bundle, evidence)
    assert outcome.ok and outcome.critique is not None
    assert outcome.chunks == len(reasoner.plan(bundle, evidence)) > 1
    assert [c.name for c in outcome.reasoning.columns] == bundle.column_names
    assert fake_llm.calls == {"semantics": outcome.chunks, "critique": outcome.chunks}
    for request in fake_llm.requests:  # every call saw only its own chunk's columns
        user = request[1].content
        raw = user.split("Table bundle (json):\n", 1)[1].split("\n}\n")[0] + "\n}"
        bundle_doc = json.loads(raw)
        assert len(bundle_doc["columns"]) <= 10


def test_merges():
    from gaia.semantics.schemas import TableCritique, TableReasoning

    def reasoning(names, hint):
        return TableReasoning.model_validate({
            "table": {"business_name": f"T{names[0]}", "purpose": "p", "confidence": 0.5},
            "columns": [{"name": n, "business_name": n, "purpose": "p", "role": "dimension",
                         "confidence": 0.5} for n in names],
            "join_hints": [hint]})
    hint = {"from_columns": ["a"], "to_table": "s.x", "to_columns": ["id"], "confidence": 0.5}
    merged = merge_reasonings([reasoning(["a", "b"], hint), reasoning(["c"], hint)])
    assert [c.name for c in merged.columns] == ["a", "b", "c"]
    assert merged.table.business_name == "Ta" and len(merged.join_hints) == 1
    crit = merge_critiques([
        TableCritique(table_verdict="plausible", table_notes=["n1"], columns=[]),
        TableCritique(table_verdict="doubtful", table_notes=["n2"], columns=[]),
    ])
    assert crit.table_verdict == "doubtful" and crit.table_notes == ["n1", "n2"]
