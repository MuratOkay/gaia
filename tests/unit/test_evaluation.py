"""Phase 3C building blocks: blinding, metrics, labels, in-memory Stage 3."""

from __future__ import annotations

import re

import pytest

from gaia.evaluation.blind import Blinder
from gaia.evaluation.labels import load_labels, stratified_sample, write_template
from gaia.evaluation.metrics import auroc, meaning_accuracy, precision_at
from gaia.evaluation.stage3 import run_claim_tests, stage1_evidence, with_tests
from gaia.profiling.summary import (
    ColumnProfileSummary,
    IdentityRelation,
    StaticEvidence,
    TableEvidenceInput,
    VerificationResult,
    ViewAlias,
)
from gaia.semantics.bundle import build_bundle, build_catalog_index
from gaia.semantics.evidence import Exposure, render_evidence
from gaia.semantics.reasoner import reasoning_messages


def _evidence(snapshot) -> StaticEvidence:
    orders = TableEvidenceInput(
        table="dbo.orders", profiling_available=True,
        columns={"status": ColumnProfileSummary("status", rows=50_000, non_null=50_000,
                                                distinct=3, top_values=[("S", 30_000)]),
                 "source_system": ColumnProfileSummary("source_system", rows=50_000,
                                                       non_null=40_000, distinct=4)},
        identities=[IdentityRelation("order_total = quantity × unit_price",
                                     ("order_total",), 1.0)],
        aliases={"status": [ViewAlias("rpt.v_daily_sales", "sales_date",
                                      "CAST(o.order_date AS date)")]},
        tests={"source_system": [VerificationResult(
            "inc:dbo.products.product_code", "inclusion", True, 0.99,
            "99% of sampled rows found in dbo.products.product_code (stage 1)", "candidate")]},
    )
    return StaticEvidence({"dbo.orders": orders})


def test_a_blind_world_carries_no_original_identifier(sample_snapshot):
    """Every schema, table, column, constraint and index name is gone from what
    the model sees — bundle, object list and evidence; data values stay."""
    blinder = Blinder(sample_snapshot)
    world = blinder.world(_evidence(sample_snapshot))
    index = build_catalog_index(world.snapshot, unique_keys=True)
    names = {n.lower() for t in sample_snapshot.tables for n in (
        t.schema, t.name, *[c.name for c in t.columns], *[k.name for k in t.constraints],
        *[i.name for i in t.indexes], *[r.name for r in t.relations])}
    names |= {"shop"}  # the database name
    for table in world.snapshot.tables:
        bundle = build_bundle(table, dialect="mssql")
        rendered = render_evidence(bundle, world.evidence.table_evidence(table.qualified_name),
                                   Exposure("raw"))
        user = reasoning_messages(bundle, index, rendered, 2)[1].content.lower()
        leaked = {n for n in names if re.search(rf"(?<![a-z0-9_]){re.escape(n)}(?![a-z0-9_])",
                                                user)}
        assert not leaked, (table.qualified_name, leaked)
    orders = world.snapshot.table(*blinder.table("dbo.orders").split("."))
    check = next(c for c in orders.constraints if c.kind == "check" and "'S'" in c.definition)
    assert "'S'" in check.definition and "'P'" in check.definition  # literals are data
    assert world.real_table(blinder.table("dbo.orders")) == "dbo.orders"
    token = blinder.column("dbo.orders", "status")
    assert world.real_column(blinder.table("dbo.orders"), token) == "status"
    ev = world.evidence.table_evidence(blinder.table("dbo.orders"))
    assert ev.columns[token].top_values == [("S", 30_000)]  # values untouched
    test = ev.tests[blinder.column("dbo.orders", "source_system")][0]
    assert test.test_id == f"inc:{blinder.table('dbo.products')}." \
                          f"{blinder.column('dbo.products', 'product_code')}"
    assert "product" not in test.detail


def test_blind_tokens_do_not_follow_the_alphabet(sample_snapshot):
    blinder = Blinder(sample_snapshot, seed=42)
    ordered = sorted(t.qualified_name for t in sample_snapshot.tables)
    tokens = [blinder.table(t) for t in ordered]
    assert tokens != sorted(tokens) or len(tokens) < 3
    assert Blinder(sample_snapshot, seed=42).table(ordered[0]) == tokens[0]  # deterministic


def test_metrics():
    assert meaning_accuracy(["yes", "partial", "no", "yes"]) == 0.625
    assert meaning_accuracy([]) is None
    assert auroc([0.9, 0.8, 0.3, 0.2], [True, True, False, False]) == 1.0
    assert auroc([0.2, 0.3, 0.8, 0.9], [True, True, False, False]) == 0.0
    assert auroc([0.5, 0.5], [True, False]) == 0.5
    assert auroc([0.5, 0.6], [True, True]) is None
    assert precision_at([0.9, 0.85, 0.4], [True, False, True], 0.8) == (0.5, 2)


def test_labels_template_and_stratified_sample(sample_snapshot, tmp_path):
    tables = [t for t in sample_snapshot.tables if t.kind == "table"]
    sample = stratified_sample(tables, "mssql", n=8, seed=1)
    assert len(sample) == 8 and sample == stratified_sample(tables, "mssql", n=8, seed=1)
    keys = {(t.qualified_name, c) for t, c in sample}
    assert ("dbo.orders", "customer_id") in keys or ("dbo.order_lines", "order_id") in keys
    path = tmp_path / "labels.csv"
    write_template(path, "demo_mssql", sample)
    assert load_labels(path) == {}  # blank rows are not labels
    text = path.read_text().splitlines()
    text[1] = text[1].replace(",,,", ",The meaning,identifier|reference,")
    path.write_text("\n".join(text) + "\n")
    (label,) = load_labels(path).values()
    assert label.role_ok("reference") and not label.role_ok("measure")
    text[1] = text[1].replace("identifier|reference", "keyish")
    path.write_text("\n".join(text) + "\n")
    with pytest.raises(ValueError, match="unknown role"):
        load_labels(path)


def test_stage1_evidence_keeps_only_what_values_said():
    ev = TableEvidenceInput(table="s.t", tests={"c": [
        VerificationResult("inc:s.p.k", "inclusion", True, 1.0, "x (stage 1)", "candidate"),
        VerificationResult("inc:s.q.k", "inclusion", True, 1.0,
                           "y (stage 1); adopted as a test of the claim", "claim"),
        VerificationResult("uniq:c", "uniqueness", False, 0.9, "z", "claim"),
    ]})
    kept = stage1_evidence(StaticEvidence({"s.t": ev})).table_evidence("s.t").tests["c"]
    assert [(t.test_id, t.origin) for t in kept] == [("inc:s.p.k", "candidate"),
                                                     ("inc:s.q.k", "candidate")]
    more = with_tests(StaticEvidence({"s.t": ev}), {"s.t": {"c": [
        VerificationResult("uniq:c", "uniqueness", True, 1.0, "new", "claim")]}})
    assert [t.detail for t in more.table_evidence("s.t").tests["c"]
            if t.test_id == "uniq:c"] == ["new"]


def test_claim_tests_run_in_memory(sample_snapshot):
    """A condition's own claims are tested read-only; nothing is stored."""
    from _fake_profiler import FakeProfiler

    from gaia.core.config import AppSection, LLMConfig, ProfilingConfig, Settings

    rows = [(i, i % 50 + 1, f"P{i % 30:03d}", 1, 9.99) for i in range(1, 3001)]
    profiler = FakeProfiler({
        "dbo.order_lines": (["order_id", "line_no", "product_code", "quantity", "unit_price"],
                            rows),
        "dbo.products": (["product_code", "product_name"],
                         [(f"P{i:03d}", f"n{i}") for i in range(30)]),
    })
    evidence = StaticEvidence({"dbo.order_lines": TableEvidenceInput(
        table="dbo.order_lines", profiling_available=True,
        columns={"product_code": ColumnProfileSummary("product_code", rows=3000,
                                                      non_null=3000, distinct=30),
                 "line_no": ColumnProfileSummary("line_no", rows=3000, non_null=3000,
                                                 distinct=50, distinct_method="estimate")})})
    notes: list[str] = []
    settings = Settings(app=AppSection(default_target="x", locale="en"), targets={},
                        llm=LLMConfig(model="fake", api_key="k"),
                        profiling=ProfilingConfig(enabled=True))
    claims = {"dbo.order_lines": {
        "product_code": ("reference", ["dbo.products.product_code"]),
        "line_no": ("code", []),
    }}
    out = run_claim_tests(settings, sample_snapshot, evidence, claims, profiler, notes)
    tests = {t.test_id: t for t in out["dbo.order_lines"]["product_code"]}
    assert tests["inc:dbo.products.product_code"].passed
    assert tests["inc:dbo.products.product_code"].origin == "claim"
    (topk,) = out["dbo.order_lines"]["line_no"]
    assert topk.test_id == "topk:line_no" and topk.passed and not notes
