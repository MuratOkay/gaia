"""Phase 3B end to end: evidence, heuristic-v2, ledger, skip key, chunks, review queue.

Runs against the disposable PostgreSQL of ``GAIA_TEST_DATABASE_URL`` (see
conftest) with the fake LLM, which under v2 prompts reads the evidence block,
cites its refs and claims detected semantic types.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from gaia.core.config import AppSection, LLMConfig, ProfilingConfig, Settings
from gaia.db.models.catalog import ColumnRecord, TableRecord
from gaia.db.models.semantics import ColumnSemantic, EvidenceItem, ReviewItem, TableSemantic
from gaia.db.repositories.snapshot_store import SnapshotStore
from gaia.profiling.summary import (
    ColumnProfileSummary,
    DetectorHit,
    NoEvidence,
    StaticEvidence,
    TableEvidenceInput,
)
from gaia.semantics.service import ReasoningOptions, run_reasoning

EMAILS = ["ayse.yilmaz12@example.com", "john.smith7@example.org", "m.kaya@example.net"]


def _settings(**llm) -> Settings:
    return Settings(
        app=AppSection(default_target="demo_mssql", locale="en"),
        targets={},
        llm=LLMConfig(model="fake-model", api_key="test", concurrency=3, **llm),
    )


def _evidence(active_distinct: int = 2, rows: int = 1200) -> StaticEvidence:
    customers = TableEvidenceInput(
        table="dbo.customers",
        profiling_available=True,
        columns={
            "email": ColumnProfileSummary(
                column="email", rows=rows, non_null=1100, distinct=1100, distinct_method="exact",
                top_values=[(EMAILS[0], 1), (EMAILS[1], 1)], sample_values=EMAILS,
                detectors=[DetectorHit("email", 0.97, 200)],
            ),
            "is_active": ColumnProfileSummary(
                column="is_active", rows=rows, non_null=rows, distinct=active_distinct,
                top_values=[("1", 1100), ("0", 100)],
            ),
            "full_name": ColumnProfileSummary(
                column="full_name", rows=rows, non_null=rows, distinct=1190,
                sample_values=["Ayse Yilmaz", "John Smith"],
            ),
        },
    )
    orders = TableEvidenceInput(
        table="dbo.orders",
        profiling_available=True,
        columns={
            "status": ColumnProfileSummary(column="status", rows=50_000, non_null=50_000,
                                           distinct=3, top_values=[("N", 30_000), ("P", 15_000),
                                                                   ("S", 5_000)]),
            "source_system": ColumnProfileSummary(column="source_system", rows=50_000,
                                                  non_null=0, distinct=0),
        },
    )
    return StaticEvidence({"dbo.customers": customers, "dbo.orders": orders})


async def _apply(factory, snapshot):
    async with factory() as s:
        await SnapshotStore(s).apply(snapshot, data_source_name="demo_mssql")
        await s.commit()


async def _reason(factory, llm, embedder, settings=None, evidence=None, **opts):
    async with factory() as s:
        return await run_reasoning(
            settings or _settings(), s, ReasoningOptions(**opts), llm_factory=lambda: llm,
            embedder=embedder, evidence_source=evidence,
        )


async def _current_columns(factory) -> dict[str, ColumnSemantic]:
    async with factory() as s:
        rows = (await s.execute(
            sa.select(ColumnSemantic, ColumnRecord, TableRecord)
            .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
            .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
            .where(ColumnSemantic.is_current.is_(True))
        )).all()
    return {f"{t.object_name}.{c.column_name}": sem for sem, c, t in rows}


async def _open_items(factory) -> dict[str, list[str]]:
    async with factory() as s:
        rows = (await s.execute(
            sa.select(ReviewItem, ColumnRecord, TableRecord)
            .join(ColumnRecord, ColumnRecord.id == ReviewItem.column_id)
            .join(TableRecord, TableRecord.id == ReviewItem.table_id)
            .where(ReviewItem.status == "open")
        )).all()
    return {f"{t.object_name}.{c.column_name}": list(r.reasons) for r, c, t in rows}


@pytest.mark.asyncio
async def test_v2_pipeline_stores_evidence_ledger_and_queue(
    pg_session_factory, snapshot_factory, fake_llm, fake_embedder
):
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    report = await _reason(factory, fake_llm, fake_embedder, evidence=_evidence())
    assert report.count("reasoned") == 5 and report.count("failed") == 0
    assert report.prompt_major == 2 and report.exposure == "masked"

    async with factory() as s:
        tables = (await s.scalars(sa.select(TableSemantic))).all()
        ledger_rows = await s.scalar(sa.select(sa.func.count()).select_from(EvidenceItem))
    assert all(t.evidence_hash and t.exposure == "masked" and t.chunks == 1 for t in tables)
    assert all(t.prompt_version.startswith("reason_table.v2+") for t in tables)
    assert ledger_rows > 0

    cols = await _current_columns(factory)
    assert all(c.weights_version == "heuristic-v2" for c in cols.values())
    email = cols["customers.email"]
    assert (email.claimed_semantic_type, email.semantic_type, email.coverage) == (
        "email", "email", "light_profile")
    ledger = email.score_breakdown["components"]["evidence_support"]["signals"]["items"]
    assert any(i["kind"] == "detector" and i["direction"] == "supports" for i in ledger)
    assert cols["orders.source_system"].unused is True
    assert cols["products.product_name"].coverage == "catalog"

    # Masked exposure: no personal value ever reached the model.
    sent = "\n".join(m.content for request in fake_llm.requests for m in request)
    for literal in [*EMAILS, "Ayse Yilmaz", "John Smith"]:
        assert literal not in sent
    assert "aaaa.aaaaaa99@a{7}.aaa" in sent
    assert '"N", "60%"' in sent.replace("\n", "").replace("  ", " ") or '"N"' in sent  # codes

    # Views after tables.
    order = [r[1].content.split('"table": "', 1)[1].split('"', 1)[0] for r in fake_llm.requests]
    first_view = order.index("rpt.v_daily_sales")
    assert all(t != "rpt.v_daily_sales" for t in order[:first_view])
    assert set(order[:first_view]) == {"dbo.customers", "dbo.orders", "dbo.order_lines",
                                      "dbo.products"}

    # Review queue: profiled tables are exhausted (no Stage 3 pending), so their
    # sub-accept columns are queued; catalog-only tables can still gain evidence.
    items = await _open_items(factory)
    assert "orders.source_system" not in items  # unused: never queued
    # Profiled columns (no Stage 3 pending) are exhausted: queued when below
    # accept. Unprofiled columns can still gain a profile: not queued unless abstain.
    exhausted_below = {k for k, c in cols.items()
                       if c.band != "accepted" and not c.unused and c.coverage == "light_profile"}
    assert exhausted_below and exhausted_below <= set(items)
    assert all(cols[k].coverage == "light_profile" or "abstain" in v for k, v in items.items())
    assert report.review["queued"] == len(items)


@pytest.mark.asyncio
async def test_raw_exposure_only_when_allowed(pg_session_factory, snapshot_factory, fake_llm,
                                              fake_embedder):
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    settings = _settings()
    settings.profiling = ProfilingConfig(allow_raw_to_remote=True)
    report = await _reason(factory, fake_llm, fake_embedder, settings=settings,
                           evidence=_evidence(), tables=["dbo.customers"])
    assert report.exposure == "raw"
    sent = "\n".join(m.content for request in fake_llm.requests for m in request)
    assert EMAILS[0] in sent


@pytest.mark.asyncio
async def test_v2_skip_key_follows_meaningful_evidence_changes(
    pg_session_factory, snapshot_factory, fake_llm, fake_embedder
):
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    await _reason(factory, fake_llm, fake_embedder, evidence=_evidence())
    first_items = await _open_items(factory)

    fake_llm.calls.clear()
    same = await _reason(factory, fake_llm, fake_embedder, evidence=_evidence())
    assert same.count("skipped") == 5 and sum(fake_llm.calls.values()) == 0

    # +0.1 % rows: every rendered figure stays in its rounding bucket. (A change
    # that crosses a bucket boundary — e.g. 8.4 % -> 8.6 % nulls — does count as
    # new evidence; that costs one re-reason of one table, by design.)
    fake_llm.calls.clear()
    noise = await _reason(factory, fake_llm, fake_embedder, evidence=_evidence(rows=1201))
    assert noise.count("skipped") == 5 and sum(fake_llm.calls.values()) == 0

    fake_llm.calls.clear()
    news = await _reason(factory, fake_llm, fake_embedder, evidence=_evidence(active_distinct=7))
    reasoned = [r for r in news.results if r.action == "reasoned"]
    assert [(r.qualified_name, r.detail) for r in reasoned] == [("dbo.customers", "")]
    assert fake_llm.calls == {"semantics": 1, "critique": 1}
    skipped_why = {r.qualified_name: r.detail for r in news.results}
    assert skipped_why["dbo.orders"].startswith("unchanged")

    # One open item per column at most; the customers items were replaced.
    async with factory() as s:
        dupes = await s.scalar(sa.text(
            "SELECT count(*) FROM (SELECT column_id FROM semantics.review_item "
            "WHERE status = 'open' GROUP BY column_id HAVING count(*) > 1) d"))
        superseded = await s.scalar(sa.select(sa.func.count()).select_from(ReviewItem)
                                    .where(ReviewItem.status == "superseded"))
    assert dupes == 0
    assert superseded == sum(1 for k in first_items if k.startswith("customers."))


@pytest.mark.asyncio
async def test_dry_run_explains_evidence_changes(pg_session_factory, snapshot_factory, fake_llm,
                                                 fake_embedder):
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    await _reason(factory, fake_llm, fake_embedder, evidence=_evidence())
    dry = await _reason(factory, fake_llm, fake_embedder, evidence=_evidence(active_distinct=7),
                        dry_run=True, show_bundles=True)
    would = {r.qualified_name: r for r in dry.results if r.action == "would_reason"}
    assert list(would) == ["dbo.customers"] and would["dbo.customers"].detail == "evidence changed"
    assert "Evidence (json):" in would["dbo.customers"].rendered


@pytest.mark.asyncio
async def test_wide_table_is_reasoned_in_chunks(pg_session_factory, snapshot_factory, fake_llm,
                                                fake_embedder):
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    settings = _settings(context_budget_tokens=2000)  # below a single orders bundle
    report = await _reason(factory, fake_llm, fake_embedder, settings=settings,
                           tables=["dbo.orders"], evidence=_evidence())
    assert report.count("reasoned") == 1
    async with factory() as s:
        table = await s.scalar(sa.select(TableSemantic).where(TableSemantic.is_current.is_(True)))
    assert table.chunks > 1
    assert fake_llm.calls == {"semantics": table.chunks, "critique": table.chunks}
    cols = await _current_columns(factory)
    assert len([k for k in cols if k.startswith("orders.")]) == 7


@pytest.mark.asyncio
async def test_catalog_only_targets_queue_for_review(pg_session_factory, snapshot_factory,
                                                     fake_llm, fake_embedder):
    """Degraded mode: profiling unavailable, so catalog coverage is final."""
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    report = await _reason(factory, fake_llm, fake_embedder, evidence=NoEvidence())
    cols = await _current_columns(factory)
    items = await _open_items(factory)
    below = {k for k, c in cols.items() if c.band != "accepted"}
    assert below == set(items)
    assert all(c.coverage == "catalog" for c in cols.values())
    assert {"evidence_exhausted", "abstain"} >= {r for v in items.values() for r in v[:1]}
    assert report.review["queued"] == len(items)


@pytest.mark.asyncio
async def test_a_failed_critique_is_retried_not_skipped(pg_session_factory, snapshot_factory,
                                                        fake_llm, fake_embedder):
    """Replay smoke test finding: a version stored with a failed critique was
    skipped as 'unchanged' forever, so its columns could never be approved."""
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    fake_llm.critique_fail_tables.add("dbo.products")
    first = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"])
    assert first.results[0].critique_ok is False

    fake_llm.critique_fail_tables.clear()
    dry = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"], dry_run=True)
    assert (dry.results[0].action, dry.results[0].detail) == ("would_reason", "critique missing")
    second = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"])
    assert second.results[0].critique_ok is True
    third = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"])
    assert third.count("skipped") == 1
