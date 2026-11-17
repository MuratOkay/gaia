"""End-to-end reasoning pipeline against a disposable PostgreSQL + pgvector.

Uses the real store, migrations-equivalent schema (``create_all``), Phase 1's
``SnapshotStore`` to seed the catalog, and the fake LLM/embedder from
``conftest``. Runs only with ``GAIA_TEST_DATABASE_URL`` set (see conftest).

Covers the PHASES Phase 2 acceptance criteria that do not need DeepSeek:
rows for every table/column, breakdown sums, zero calls on unchanged input,
one-column change re-reasons one bundle, failures retained with raw payloads,
embeddings with model name, and score distribution reporting.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from gaia.core.config import AppSection, LLMConfig, ScoringConfig, SemanticsConfig, Settings
from gaia.db.models.audit import LLMCall
from gaia.db.models.semantics import (
    ColumnSemantic,
    Embedding,
    JoinHint,
    ReasoningRun,
    TableSemantic,
)
from gaia.db.repositories.snapshot_store import SnapshotStore
from gaia.semantics.service import ReasoningOptions, run_reasoning


def _settings() -> Settings:
    """Phase 2 behaviour (prompt generation 1, heuristic-v1): these tests are
    the regression net for it; Phase 3B is covered by test_reasoning_v2_pg.py."""
    return Settings(
        app=AppSection(default_target="demo_mssql", locale="en"),
        targets={},
        llm=LLMConfig(model="deepseek-v4.1-flash", api_key="test", concurrency=3),
        semantics=SemanticsConfig(prompt_major=1),
        scoring=ScoringConfig(version="heuristic-v1"),
    )


async def _apply(factory, snapshot):
    async with factory() as s:
        await SnapshotStore(s).apply(snapshot, data_source_name="demo_mssql")
        await s.commit()


async def _reason(factory, llm, embedder, **opts):
    async with factory() as s:
        return await run_reasoning(
            _settings(),
            s,
            ReasoningOptions(**opts),
            llm_factory=lambda: llm,
            embedder=embedder,
        )


async def _count(factory, stmt) -> int:
    async with factory() as s:
        return int(await s.scalar(stmt))


TOTAL_COLUMNS = 5 + 7 + 5 + 2 + 2
TOTAL_TABLES = 5


@pytest.mark.asyncio
async def test_pipeline_end_to_end(pg_session_factory, snapshot_factory, fake_llm, fake_embedder):
    factory = pg_session_factory
    build_sample_snapshot = snapshot_factory
    await _apply(factory, build_sample_snapshot())

    # -- dry run writes nothing and calls nothing ---------------------------------
    dry = await _reason(factory, fake_llm, fake_embedder, dry_run=True, show_bundles=True)
    assert dry.count("would_reason") == TOTAL_TABLES
    assert sum(fake_llm.calls.values()) == 0
    assert await _count(factory, sa.select(sa.func.count()).select_from(ReasoningRun)) == 0
    assert any("Table bundle (json)" in (r.rendered or "") for r in dry.results)

    # -- first run ------------------------------------------------------------------
    report = await _reason(factory, fake_llm, fake_embedder)
    assert report.count("reasoned") == TOTAL_TABLES
    assert report.count("failed") == 0
    assert fake_llm.calls == {"semantics": TOTAL_TABLES, "critique": TOTAL_TABLES}

    async with factory() as s:
        tables = (await s.scalars(sa.select(TableSemantic))).all()
        columns = (await s.scalars(sa.select(ColumnSemantic))).all()
        calls = (await s.scalars(sa.select(LLMCall))).all()
        run = await s.scalar(sa.select(ReasoningRun))
        hints = (await s.scalars(sa.select(JoinHint))).all()
        embeddings = (await s.scalars(sa.select(Embedding))).all()

    assert len(tables) == TOTAL_TABLES and all(t.is_current and t.version == 1 for t in tables)
    assert len(columns) == TOTAL_COLUMNS and all(c.is_current for c in columns)
    for c in columns:
        parts = c.score_breakdown["components"]
        assert round(sum(p["contribution"] for p in parts.values()), 6) == c.raw_score
        assert c.calibrated_score == c.raw_score  # identity calibrator
        assert c.status == "draft"  # nothing auto-approves without comments/profiles
    assert {c.band for c in columns} >= {"deep_profile", "deep_profile_flagged"}  # real spread

    assert len(calls) == 2 * TOTAL_TABLES and all(c.parsed_ok for c in calls)
    assert run.status == "completed" and run.llm_calls == 2 * TOTAL_TABLES
    assert run.prompt_tokens == 1000 * 2 * TOTAL_TABLES

    fk = sorted((h.from_table, h.to_table) for h in hints if h.source == "fk")
    assert fk == [("order_lines", "orders"), ("orders", "customers")]
    assert all(h.status == "confirmed" for h in hints if h.source == "fk")
    inferred = [h for h in hints if h.source == "llm"]
    assert [(h.from_table, h.to_table, h.status) for h in inferred] == [
        ("order_lines", "products", "inferred")
    ]

    assert len(embeddings) == TOTAL_TABLES + TOTAL_COLUMNS
    assert all(e.model == "bge-m3" and e.dimensions == 1024 for e in embeddings)
    assert len(embeddings[0].embedding) == 1024

    assert report.distribution["count"] == TOTAL_COLUMNS
    assert sum(n for _, n in report.histogram) == TOTAL_COLUMNS

    # -- unchanged re-run: zero LLM calls, zero writes --------------------------------
    fake_llm.calls.clear()
    again = await _reason(factory, fake_llm, fake_embedder)
    assert again.count("skipped") == TOTAL_TABLES
    assert sum(fake_llm.calls.values()) == 0
    assert await _count(factory, sa.select(sa.func.count()).select_from(ColumnSemantic)) == (
        TOTAL_COLUMNS
    )
    assert await _count(factory, sa.select(sa.func.count()).select_from(Embedding)) == (
        TOTAL_TABLES + TOTAL_COLUMNS
    )

    # -- one column changes: only its table bundle is re-reasoned ---------------------
    changed = build_sample_snapshot()
    orders = next(t for t in changed.tables if t.name == "orders")
    next(c for c in orders.columns if c.name == "notes").data_type = "nvarchar(500)"
    await _apply(factory, changed)

    fake_llm.calls.clear()
    third = await _reason(factory, fake_llm, fake_embedder)
    assert [r.qualified_name for r in third.results if r.action == "reasoned"] == ["dbo.orders"]
    assert fake_llm.calls == {"semantics": 1, "critique": 1}

    async with factory() as s:
        versions = (
            await s.execute(
                sa.select(TableSemantic.version, TableSemantic.is_current, TableSemantic.status)
                .where(TableSemantic.business_name == "Orders")
                .order_by(TableSemantic.version)
            )
        ).all()
        current_cols = await s.scalar(
            sa.select(sa.func.count())
            .select_from(ColumnSemantic)
            .where(ColumnSemantic.is_current.is_(True))
        )
        new_embeddings = await _count(factory, sa.select(sa.func.count()).select_from(Embedding))
    assert versions == [(1, False, "stale"), (2, True, "draft")]
    assert current_cols == TOTAL_COLUMNS
    assert new_embeddings == TOTAL_TABLES + TOTAL_COLUMNS + 1 + 7

    # -- forced failure: recorded with raw payload, previous version stays current -----
    fake_llm.fail_tables.add("dbo.products")
    failing = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"], force=True)
    assert failing.count("failed") == 1
    async with factory() as s:
        bad = (await s.scalars(sa.select(LLMCall).where(LLMCall.parsed_ok.is_(False)))).all()
        run_status = await s.scalar(
            sa.select(ReasoningRun.status).where(
                ReasoningRun.id == sa.cast(failing.run_id, sa.Uuid)
            )
        )
        products_current = await s.scalar(
            sa.select(TableSemantic.version).where(
                TableSemantic.business_name == "Products", TableSemantic.is_current.is_(True)
            )
        )
    assert len(bad) == 2 and all(b.raw_response == "this is not json" for b in bad)
    assert run_status == "failed"
    assert products_current == 1

    # -- critique failure: reasoning stored, counter_evidence not assessed -------------
    fake_llm.fail_tables.clear()
    fake_llm.critique_fail_tables.add("dbo.products")
    partial = await _reason(factory, fake_llm, fake_embedder, tables=["dbo.products"], force=True)
    assert partial.count("reasoned") == 1
    assert partial.results[0].critique_ok is False
    async with factory() as s:
        cols = (
            await s.scalars(
                sa.select(ColumnSemantic)
                .join(TableSemantic, TableSemantic.id == ColumnSemantic.table_semantic_id)
                .where(
                    TableSemantic.business_name == "Products", ColumnSemantic.is_current.is_(True)
                )
            )
        ).all()
        status = await s.scalar(
            sa.select(ReasoningRun.status).where(
                ReasoningRun.id == sa.cast(partial.run_id, sa.Uuid)
            )
        )
    assert status == "completed_with_failures"
    assert len(cols) == 2
    assert all(
        c.score_breakdown["components"]["counter_evidence"]["signals"]["verdict"] == "not_assessed"
        for c in cols
    )

    # -- a dropped table's semantics become stale ---------------------------------------
    dropped = build_sample_snapshot()
    dropped.tables = [t for t in dropped.tables if t.name != "products"]
    await _apply(factory, dropped)
    await _reason(factory, fake_llm, fake_embedder)
    async with factory() as s:
        products_status = await s.scalar(
            sa.select(TableSemantic.status).where(
                TableSemantic.business_name == "Products", TableSemantic.is_current.is_(True)
            )
        )
    assert products_status == "stale"


@pytest.mark.asyncio
async def test_single_column_change_reasons_exactly_one_bundle(
    pg_session_factory, snapshot_factory, fake_llm, fake_embedder
):
    """PHASES Phase 2 acceptance, closed in 3.0: one column edit re-reasons
    exactly its table bundle; every other table keeps its version; data-only
    movement re-reasons nothing."""
    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    await _reason(factory, fake_llm, fake_embedder)

    async def current_versions() -> dict[str, tuple[int, str]]:
        async with factory() as s:
            rows = (
                await s.execute(
                    sa.select(
                        TableSemantic.business_name, TableSemantic.version, TableSemantic.status
                    ).where(TableSemantic.is_current.is_(True))
                )
            ).all()
        return {name: (version, status) for name, version, status in rows}

    baseline = await current_versions()
    assert len(baseline) == TOTAL_TABLES and all(v == 1 for v, _ in baseline.values())

    # Data-only movement: rows x10 and a modification counter. No re-reasoning.
    grown = snapshot_factory()
    for t in grown.tables:
        t.estimated_rows = (t.estimated_rows or 0) * 10 + 1
        t.modification_counter = 12345
    await _apply(factory, grown)
    fake_llm.calls.clear()
    data_only = await _reason(factory, fake_llm, fake_embedder)
    assert data_only.count("reasoned") == 0 and data_only.count("skipped") == TOTAL_TABLES
    assert sum(fake_llm.calls.values()) == 0
    assert await current_versions() == baseline

    # A type change on one column of one table.
    typed = snapshot_factory()
    orders = next(t for t in typed.tables if t.name == "orders")
    next(c for c in orders.columns if c.name == "notes").data_type = "nvarchar(1000)"
    await _apply(factory, typed)
    fake_llm.calls.clear()
    one = await _reason(factory, fake_llm, fake_embedder)
    assert [r.qualified_name for r in one.results if r.action == "reasoned"] == ["dbo.orders"]
    assert one.count("skipped") == TOTAL_TABLES - 1
    assert fake_llm.calls == {"semantics": 1, "critique": 1}
    after = await current_versions()
    assert after.pop("Orders") == (2, "draft")
    assert after == {k: v for k, v in baseline.items() if k != "Orders"}

    # A comment-only edit on one column of another table: also exactly one bundle.
    commented = snapshot_factory()
    orders = next(t for t in commented.tables if t.name == "orders")
    next(c for c in orders.columns if c.name == "notes").data_type = "nvarchar(1000)"
    products = next(t for t in commented.tables if t.name == "products")
    products.columns[0].comment = "Product surrogate key"
    await _apply(factory, commented)
    fake_llm.calls.clear()
    two = await _reason(factory, fake_llm, fake_embedder)
    assert [r.qualified_name for r in two.results if r.action == "reasoned"] == ["dbo.products"]
    assert fake_llm.calls == {"semantics": 1, "critique": 1}
    final = await current_versions()
    assert final["Products"] == (2, "draft") and final["Orders"] == (2, "draft")
    assert {k: v for k, v in final.items() if k not in ("Products", "Orders")} == {
        k: v for k, v in baseline.items() if k not in ("Products", "Orders")
    }


@pytest.mark.asyncio
async def test_one_writing_run_per_data_source(
    pg_session_factory, snapshot_factory, fake_llm, fake_embedder
):
    """5.0 (T2): while one run holds a data source, a second writing run — reasoning or
    re-scoring — is refused before it plans or calls the model; a dry run is not; the lock
    is released when the first run ends, whether it succeeded or not. Profiling keeps its
    own lock (D33), so the two kinds do not exclude each other."""
    from gaia.db.locks import DataSourceLock
    from gaia.semantics.service import ReasoningBusy

    factory = pg_session_factory
    await _apply(factory, snapshot_factory())
    async with factory() as s:
        ds_id = await s.scalar(
            sa.text("SELECT id FROM catalog.data_sources WHERE name = 'demo_mssql'"))

    async with factory() as holder, DataSourceLock(holder, ds_id, "reasoning"):
        with pytest.raises(ReasoningBusy, match="one run per data source"):
            await _reason(factory, fake_llm, fake_embedder)
        with pytest.raises(ReasoningBusy):
            await _reason(factory, fake_llm, fake_embedder, rescore_only=True)
        assert sum(fake_llm.calls.values()) == 0
        assert await _count(factory, sa.select(sa.func.count()).select_from(ReasoningRun)) == 0
        dry = await _reason(factory, fake_llm, fake_embedder, dry_run=True)
        assert dry.count("would_reason") == TOTAL_TABLES
        # a profiling run of the same data source is not blocked by reasoning
        async with DataSourceLock(holder, ds_id, "profiling"):
            pass

    report = await _reason(factory, fake_llm, fake_embedder)  # released: runs
    assert report.count("reasoned") == TOTAL_TABLES

    # a failing run releases the lock too
    class Boom(Exception):
        pass

    def broken():
        raise Boom("no client")

    async with factory() as s:
        with pytest.raises(Boom):
            await run_reasoning(_settings(), s, ReasoningOptions(force=True),
                                llm_factory=broken, embedder=fake_embedder)
    again = await _reason(factory, fake_llm, fake_embedder)
    assert again.count("skipped") == TOTAL_TABLES
