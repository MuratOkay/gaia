"""Phase 3A end to end on the PostgreSQL development target (``demo_pg``).

Needs both opt-in databases:

* ``GAIA_TEST_PG_TARGET_URL`` — the read-only role on a database seeded by
  ``infra/pg-target/seed.py`` (any ``--scale``);
* ``GAIA_TEST_DATABASE_URL`` — the disposable metadata store (see conftest).

Covers the 3A acceptance criteria that can be proven on PostgreSQL: every
table profiled or skipped with a reason, per-metric provenance, budgets,
view lineage, join candidates, zero data queries on reuse, no footprint,
catalog-only mode when SELECT is denied, the run window, and Stage 3 tests of
claims followed by re-reasoning of exactly the tables whose evidence changed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

import pytest
import sqlalchemy as sa

from gaia.core.config import (
    AppSection,
    LLMConfig,
    ProfilingBudget,
    ProfilingConfig,
    Settings,
    TargetConfig,
)
from gaia.db.models.catalog import ColumnRecord, TableRecord
from gaia.db.models.profiling import TableProfile, VerificationTest
from gaia.db.models.semantics import ColumnSemantic, EvidenceItem, JoinHint
from gaia.db.repositories.profile_store import ProfileStore
from gaia.db.repositories.snapshot_store import SnapshotStore
from gaia.dialects.postgres.profiler import PostgresProfiler
from gaia.profiling.service import ProfileOptions, ProfilingError, run_profiling, run_stage3
from gaia.semantics.evidence import next_stage
from gaia.semantics.service import ReasoningOptions, run_reasoning

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")


def _target() -> TargetConfig:
    parts = urlsplit(URL)
    return TargetConfig(
        dialect="postgres", host=parts.hostname or "127.0.0.1", port=parts.port or 5432,
        database=parts.path.lstrip("/"), username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


def _settings(**budget) -> Settings:
    return Settings(
        app=AppSection(default_target="demo_pg", locale="en"),
        targets={"demo_pg": _target()},
        llm=LLMConfig(model="fake-model", api_key="test", concurrency=3),
        profiling=ProfilingConfig(enabled=True, budget=ProfilingBudget(**budget)),
    )


class Recording(PostgresProfiler):
    """The real profiler, recording every statement; optionally denying SELECT."""

    denied: set[str] = set()

    def __init__(self, target, budget):
        super().__init__(target, budget)
        self.statements: list[str] = []

    def query(self, cur, sql, params=None, limit=None):
        self.statements.append(sql)
        return super().query(cur, sql, params, limit)

    def can_select(self, cur, schema, name):
        if f"{schema}.{name}" in self.denied:
            return False
        return super().can_select(cur, schema, name)


def _factory(holder: list, denied: set[str] | None = None):
    def make(target, settings):
        profiler = Recording(target, settings.profiling.budget)
        profiler.denied = denied or set()
        holder.append(profiler)
        return profiler

    return make


async def _introspect(factory):
    from gaia.dialects.postgres.catalog import PostgresCatalogReader

    snapshot = PostgresCatalogReader(_target()).read_snapshot(data_source="demo_pg")
    async with factory() as s:
        await SnapshotStore(s).apply(snapshot, data_source_name="demo_pg")
        await s.commit()
    return snapshot


async def _current_profiles(factory) -> dict[str, TableProfile]:
    async with factory() as s:
        rows = (await s.execute(
            sa.select(TableProfile, TableRecord)
            .join(TableRecord, TableRecord.id == TableProfile.table_id)
            .where(TableProfile.is_current.is_(True))
        )).all()
    return {f"{t.schema_name}.{t.object_name}": p for p, t in rows}


@pytest.mark.asyncio
async def test_stage1_profiles_every_table_and_reuses_without_data_queries(pg_session_factory):
    factory = pg_session_factory
    snapshot = await _introspect(factory)
    settings = _settings()
    made: list[Recording] = []
    async with factory() as s:
        report = await run_profiling(settings, s, ProfileOptions(target="demo_pg"),
                                     profiler_factory=_factory(made))

    # every in-scope table: a profile or a recorded skip reason
    profiles = await _current_profiles(factory)
    assert set(profiles) == {t.qualified_name for t in snapshot.tables}
    for name, p in profiles.items():
        kind = snapshot.table(*name.split(".")).kind
        if kind == "view":
            assert (p.status, p.skip_reason) == ("skipped", "view"), name
        else:
            assert p.status == "profiled", (name, p.status, p.notes)
            budget = settings.profiling.budget
            assert p.pages_estimated <= budget.max_pages_per_table
            assert p.runtime_ms <= budget.max_runtime_per_table_s * 1000
    assert report.count("profiled") == 24 and report.count("skipped") == 4

    # every metric: method, rows scanned, pages, runtime, isolation; seed when sampled
    async with factory() as s:
        from gaia.db.models.profiling import ColumnProfile

        metrics = (await s.scalars(sa.select(ColumnProfile.metrics))).all()
    checked = 0
    for per_column in metrics:
        for entry in per_column.values():
            assert entry["method"] and entry["isolation"] == "read_committed"
            if "skipped" in entry or "error" in entry:
                continue
            assert {"rows_scanned", "pages", "runtime_ms"} <= set(entry), entry
            if entry["method"] == "sampled_pages":
                assert entry["seed"] == 42
            checked += 1
    assert checked > 400

    # view lineage: every view column classified; the legacy view by hand
    assert report.lineage == {"pass_through": 16, "computed": 12, "unresolved": 0}
    async with factory() as s:
        evidence = await ProfileStore(s).load_evidence(
            (await s.scalar(sa.select(TableRecord.data_source_id).limit(1))), snapshot,
            enabled=True)
    legacy = evidence.table_evidence("legacy.tb_trx02")
    aliases = {col: [a.alias for a in items] for col, items in legacy.aliases.items()}
    assert aliases == {"f1": ["transaction_id"], "f2": ["customer_code"],
                       "f3": ["transaction_date"], "f5": ["quantity"], "f6": ["unit_price"],
                       "f7": ["amount"], "f8": ["status_code"], "f9": ["store_code"]}
    assert legacy.aliases["f3"][0].expression.startswith("TO_DATE(")  # converted, not passed
    view = evidence.table_evidence("rpt.v_legacy_transactions")
    assert view.lineage["amount"].source == "legacy.tb_trx02.f7"
    assert view.lineage["transaction_date"].classification == "computed"
    assert not view.profiling_available  # views are never sampled
    assert any(i.expression == "f7 = f5 × f6" for i in legacy.identities)
    assert any(d.semantic_type == "date_int" for d in legacy.columns["f3"].detectors)

    # join candidates from values alone; a time of day is not a reference
    joins = {(j["child"], j["parent"]): j for j in report.joins}
    for child, parent in [("legacy.tb_trx02.f2", "crm.customers.customer_code"),
                          ("legacy.tb_trx02.f9", "store.stores.store_code"),
                          ("legacy.tb_trx02.f10", "finance.currencies.currency_code")]:
        assert joins[(child, parent)]["passed"], (child, parent)
    assert not any(child == "legacy.tb_trx02.f4" for child, _ in joins)
    assert [t.test_id for t in legacy.tests["f2"]] == ["inc:crm.customers.customer_code"]

    # PostgreSQL: no statistics appear (only ANALYZE creates them)
    assert report.footprint is not None and report.footprint["new"] == []

    # second run inside the freshness window: zero data queries
    made.clear()
    async with factory() as s:
        again = await run_profiling(settings, s, ProfileOptions(target="demo_pg"),
                                    profiler_factory=_factory(made))
    assert again.count("reused") == 24 and again.queries == 0
    user_schemas = re.compile(r'"(crm|sales|legacy|finance|hr|product|store|rpt)"\."')
    assert not [q for q in made[0].statements if user_schemas.search(q)]


@pytest.mark.asyncio
async def test_select_denied_degrades_to_catalog_only(pg_session_factory):
    factory = pg_session_factory
    snapshot = await _introspect(factory)
    denied = {"legacy.tb_trx02", "legacy.tb_mst01"}
    async with factory() as s:
        report = await run_profiling(
            _settings(), s, ProfileOptions(target="demo_pg", schemas=["legacy"]),
            profiler_factory=_factory([], denied))
    actions = {r.qualified_name: (r.action, r.detail) for r in report.results}
    assert actions["legacy.tb_trx02"] == ("skipped", "no_select")
    assert actions["legacy.tb_prm03"][0] == "profiled"
    async with factory() as s:
        ds = await s.scalar(sa.select(TableRecord.data_source_id).limit(1))
        evidence = await ProfileStore(s).load_evidence(ds, snapshot, enabled=True)
    trx = evidence.table_evidence("legacy.tb_trx02")
    assert not trx.profiling_available and not trx.columns
    assert next_stage("modules", trx, trx.profiling_available) is None  # exhausted: review


@pytest.mark.asyncio
async def test_window_and_disabled_profiling_refuse(pg_session_factory):
    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    settings.profiling.window = "01:00-02:00"
    noon = datetime(2026, 11, 14, 12, 0)
    async with factory() as s:
        with pytest.raises(ProfilingError, match="outside the profiling window"):
            await run_profiling(settings, s, ProfileOptions(target="demo_pg"), now=noon)
        plan = await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", ignore_window=True, dry_run=True), now=noon)
        assert plan.count("would_profile") == 24
        settings.targets["demo_pg"].profiling_enabled = False
        with pytest.raises(ProfilingError, match="disabled"):
            await run_profiling(settings, s, ProfileOptions(target="demo_pg", dry_run=True))


@pytest.mark.asyncio
async def test_stage3_tests_claims_then_reasoning_reruns_only_changed_tables(
    pg_session_factory, fake_llm, fake_embedder
):
    factory = pg_session_factory
    await _introspect(factory)
    # every table "large": distinct counts of free columns are estimates
    settings = _settings(full_scan_max_pages=50)
    scope = ["legacy.tb_trx02", "crm.customers", "sales.orders", "hr.employees"]
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg", tables=scope))

    async def reason():
        async with factory() as s:
            return await run_reasoning(settings, s, ReasoningOptions(target="demo_pg",
                                                                     tables=scope),
                                       llm_factory=lambda: fake_llm, embedder=fake_embedder)

    first = await reason()
    assert first.count("reasoned") == 4

    # Claims to test: f2 as an identifier (it is not unique), f4 as a code (it has
    # tens of thousands of values), and two joins the "model" proposed.
    async with factory() as s:
        ids = {f"{t.schema_name}.{t.object_name}.{c.column_name}": c.id for c, t in (
            await s.execute(sa.select(ColumnRecord, TableRecord).join(
                TableRecord, TableRecord.id == ColumnRecord.table_id))).all()}
        for column, role in [("legacy.tb_trx02.f2", "identifier"), ("legacy.tb_trx02.f4", "code")]:
            await s.execute(sa.update(ColumnSemantic).where(
                ColumnSemantic.column_id == ids[column], ColumnSemantic.is_current.is_(True)
            ).values(role=role))
        ds = await s.scalar(sa.select(TableRecord.data_source_id).limit(1))
        for src, dst in [("legacy.tb_trx02.f2", "crm.customers.customer_code"),
                         ("sales.orders.sales_rep_id", "hr.employees.employee_id")]:
            fs, ft, fc = src.split(".")
            ts, tt, tc = dst.split(".")
            s.add(JoinHint(data_source_id=ds, from_schema=fs, from_table=ft, from_columns=[fc],
                           to_schema=ts, to_table=tt, to_columns=[tc], source="llm",
                           status="inferred", confidence=0.7))
        await s.commit()

    async with factory() as s:
        plan = await run_stage3(settings, s, ProfileOptions(target="demo_pg", tables=scope,
                                                             dry_run=True))
    planned = {(r.qualified_name, r.detail.split(" ")[0]) for r in plan.results}
    assert planned == {
        ("legacy.tb_trx02.f2", "inc:crm.customers.customer_code"),
        ("legacy.tb_trx02.f2", "uniq:f2"),
        ("legacy.tb_trx02.f4", "topk:f4"),
        ("sales.orders.sales_rep_id", "inc:hr.employees.employee_id"),
    }
    async with factory() as s:
        report = await run_stage3(settings, s, ProfileOptions(target="demo_pg", tables=scope))
    results = {(t["column"], t["test"]): t for t in report.tests}
    assert results[("legacy.tb_trx02.f2", "inc:crm.customers.customer_code")]["method"] == (
        "adopted")  # the Stage 1 candidate becomes the test of the claim: no query
    assert not results[("legacy.tb_trx02.f2", "uniq:f2")]["passed"]
    assert not results[("legacy.tb_trx02.f4", "topk:f4")]["passed"]
    rep = results[("sales.orders.sales_rep_id", "inc:hr.employees.employee_id")]
    assert rep["passed"]

    # every inferred join hint now has an inclusion-test result
    async with factory() as s:
        tests = (await s.scalars(sa.select(VerificationTest).where(
            VerificationTest.is_current.is_(True), VerificationTest.origin == "claim"))).all()
    by_key = {(t.column_name, t.test_id): t for t in tests}
    assert ("f2", "inc:crm.customers.customer_code") in by_key
    weak = by_key[("sales_rep_id", "inc:hr.employees.employee_id")]
    assert weak.specific is False  # employee ids are dense: a pass proves little

    # nothing is pending any more: a second Stage 3 run has nothing to do
    async with factory() as s:
        again = await run_stage3(settings, s, ProfileOptions(target="demo_pg", tables=scope,
                                                              dry_run=True))
    assert again.results == []

    # re-reasoning touches exactly the tables whose evidence changed
    second = await reason()
    actions = {r.qualified_name: (r.action, r.detail) for r in second.results}
    assert actions["legacy.tb_trx02"] == ("reasoned", "") or actions["legacy.tb_trx02"][0] == (
        "reasoned")
    assert actions["sales.orders"][0] == "reasoned"
    assert actions["crm.customers"][0] == "skipped" and actions["hr.employees"][0] == "skipped"
    async with factory() as s:
        kinds = set((await s.scalars(sa.select(EvidenceItem.kind))).all())
    assert "test_inclusion" in kinds
