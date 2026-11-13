"""Reasoner: prompt layout, schema validation, join-hint resolution, critique."""

from __future__ import annotations

from gaia.semantics.bundle import build_bundle, build_catalog_index
from gaia.semantics.critique import critique_prompt
from gaia.semantics.prompts import load_prompt
from gaia.semantics.reasoner import (
    ReasonerSettings,
    TableReasoner,
    reasoning_messages,
    reasoning_prompt,
)
from gaia.semantics.schemas import ColumnMeaning, TableReasoning, check_reasoning
from gaia.semantics.service import declared_joins

SETTINGS = ReasonerSettings(
    model="deepseek-v4.1-flash",
    thinking=True,
    critique_thinking=True,
    max_tokens=16000,
    temperature=0.1,
    validation_retries=1,
)


def _reasoner(snapshot, llm, **kw):
    return TableReasoner(
        llm,
        SETTINGS,
        build_catalog_index(snapshot),
        declared_joins=declared_joins(snapshot),
        **kw,
    )


def _bundle(snapshot, qualified):
    return build_bundle(next(t for t in snapshot.tables if t.qualified_name == qualified))


def test_prompt_prefix_is_byte_identical_across_tables(sample_snapshot):
    index = build_catalog_index(sample_snapshot)
    a = reasoning_messages(_bundle(sample_snapshot, "dbo.orders"), index)
    b = reasoning_messages(_bundle(sample_snapshot, "dbo.customers"), index)

    assert a[0].content == b[0].content  # system prompt
    split = "Table bundle (json):\n"
    prefix_a = a[1].content.split(split)[0]
    prefix_b = b[1].content.split(split)[0]
    assert prefix_a == prefix_b  # object list precedes the volatile bundle
    assert "dbo.products" in prefix_a


def test_prompt_versions_carry_content_hash():
    p = reasoning_prompt()
    assert p.version.startswith("reason_table.v1+")
    assert len(p.version.split("+")[1]) == 10
    assert critique_prompt().version.startswith("critique_table.v1+")
    assert load_prompt("reason_table", 1) is p  # cached
    assert "json" in p.text.lower()  # JSON mode requires the word


def test_full_table_outcome(sample_snapshot, fake_llm):
    outcome = _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.orders"))

    assert outcome.ok and outcome.error is None
    assert [c.name for c in outcome.reasoning.columns] == [
        "order_id",
        "customer_id",
        "order_date",
        "order_total",
        "status",
        "notes",
        "source_system",
    ]
    assert outcome.critique is not None
    assert [c.purpose for c in outcome.calls] == ["semantics", "critique"]
    assert fake_llm.calls == {"semantics": 1, "critique": 1}


def test_missing_column_is_retried_with_feedback(sample_snapshot, fake_llm):
    fake_llm.heal_tables.add("dbo.orders")
    outcome = _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.orders"))

    assert outcome.ok
    semantics_calls = [c for c in outcome.calls if c.purpose == "semantics"]
    assert [c.parsed_ok for c in semantics_calls] == [False, True]
    assert "order_id" in semantics_calls[0].error


def test_reasoning_failure_keeps_calls(sample_snapshot, fake_llm):
    fake_llm.fail_tables.add("dbo.orders")
    outcome = _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.orders"))

    assert not outcome.ok
    assert "no schema-valid response" in outcome.error
    assert len(outcome.calls) == 2  # first try + 1 validation retry
    assert all(c.content == "this is not json" for c in outcome.calls)
    assert fake_llm.calls["critique"] == 0


def test_critique_failure_keeps_reasoning(sample_snapshot, fake_llm):
    fake_llm.critique_fail_tables.add("dbo.orders")
    outcome = _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.orders"))

    assert outcome.ok
    assert outcome.critique is None
    assert "columns missing" in outcome.critique_error


def test_join_hints_resolved_against_catalog(sample_snapshot, fake_llm):
    outcome = _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.order_lines"))

    assert len(outcome.join_hints) == 1
    hint = outcome.join_hints[0]
    assert (hint.from_table, hint.from_columns, hint.to_table, hint.to_columns) == (
        "order_lines",
        ["product_code"],
        "products",
        ["product_code"],
    )
    dropped = " ".join(outcome.notes)
    assert "already a declared foreign key" in dropped
    assert "unknown target table" in dropped


def test_critique_sees_claims_not_confidences(sample_snapshot, fake_llm):
    _reasoner(sample_snapshot, fake_llm).run(_bundle(sample_snapshot, "dbo.orders"))
    critique_request = next(
        r for r in fake_llm.requests if r[0].content.startswith("You are a sceptical reviewer")
    )
    claims = critique_request[1].content.split("Proposed interpretation to review (json):\n")[1]
    assert '"business_name"' in claims
    assert '"confidence"' not in claims
    assert '"evidence"' not in claims


def test_column_names_are_canonicalised(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.products")
    reasoning = TableReasoning(
        table={"business_name": "P", "purpose": "p", "confidence": 0.5},
        columns=[
            ColumnMeaning(
                name="PRODUCT_NAME",
                business_name="n",
                purpose="p",
                role="dimension",
                confidence=0.5,
            ),
            ColumnMeaning(
                name="Product_Code",
                business_name="c",
                purpose="p",
                role="identifier",
                confidence=0.5,
            ),
        ],
    )
    assert check_reasoning(reasoning, bundle.column_names) == []
    from gaia.semantics.reasoner import _canonical_names

    fixed = _canonical_names(reasoning, bundle)
    assert [c.name for c in fixed.columns] == ["product_code", "product_name"]


def test_coverage_checker_reports_missing_extra_and_duplicates():
    def cm(n):
        return ColumnMeaning(
            name=n, business_name="x", purpose="p", role="dimension", confidence=0.5
        )

    value = TableReasoning(
        table={"business_name": "T", "purpose": "p", "confidence": 0.5},
        columns=[cm("a"), cm("a"), cm("zzz")],
    )
    errors = " | ".join(check_reasoning(value, ["a", "b"]))
    assert "missing" in errors and "b" in errors
    assert "not in the table" in errors and "zzz" in errors
    assert "more than once" in errors
