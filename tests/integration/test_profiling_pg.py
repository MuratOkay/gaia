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
from gaia.db.models.semantics import (
    ColumnSemantic,
    EvidenceItem,
    JoinHint,
    ReviewItem,
    TableSemantic,
)
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
        coverage = await s.scalar(sa.select(ColumnSemantic.coverage).where(
            ColumnSemantic.column_id == ids["legacy.tb_trx02.f2"],
            ColumnSemantic.is_current.is_(True)))
        kinds = {(k, d) for k, d in (await s.execute(
            sa.select(EvidenceItem.kind, EvidenceItem.direction))).all()}
    assert coverage == "targeted"
    # the fake model calls f2 a dimension: values found in a key say nothing
    # about a name (evidence.JOIN_NEUTRAL_ROLES), so no test item contradicts it
    assert ("test_inclusion", "contradicts") not in kinds


@pytest.mark.asyncio
async def test_join_hints_follow_the_model_and_the_tests(
    pg_session_factory, fake_llm, fake_embedder
):
    """Owner run 2026-11-15, three defects on one path:

    * the model aimed f2 / f9 (codes) at integer primary keys; the conversion
      error left those claims untested forever (D1) -> they now fail and the
      hints are ``refuted``;
    * hints from an earlier answer stayed ``inferred`` after the model had moved
      on (D2) -> re-reasoning a table stales the hints it no longer proposes;
    * a claim test that cannot run is recorded as inconclusive, so the column is
      no longer "pending" forever (D1).
    """
    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    scope = ["legacy.tb_trx02", "crm.customers", "store.stores", "legacy.xx_aux"]
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg", tables=scope))

    async def reason():
        async with factory() as s:
            return await run_reasoning(settings, s, ReasoningOptions(target="demo_pg",
                                                                     tables=scope),
                                       llm_factory=lambda: fake_llm, embedder=fake_embedder)

    await reason()
    hints = [("legacy.tb_trx02.f2", "crm.customers.customer_id"),
             ("legacy.tb_trx02.f9", "store.stores.store_id"),
             ("legacy.xx_aux.id", "crm.customers.customer_id")]
    async with factory() as s:
        ds = await s.scalar(sa.select(TableRecord.data_source_id).limit(1))
        for src, dst in hints:
            fs, ft, fc = src.split(".")
            ts, tt, tc = dst.split(".")
            s.add(JoinHint(data_source_id=ds, from_schema=fs, from_table=ft, from_columns=[fc],
                           to_schema=ts, to_table=tt, to_columns=[tc], source="llm",
                           status="inferred", confidence=0.6))
        await s.commit()

    # 1. type-mismatched claims are tested and fail; xx_aux.id is refused by a
    #    table page budget of 1 page and recorded as "not run"
    async with factory() as s:
        report = await run_stage3(settings, s, ProfileOptions(
            target="demo_pg", tables=["legacy.tb_trx02"]))
    results = {(t["column"], t["test"]): t for t in report.tests}
    f2 = results[("legacy.tb_trx02.f2", "inc:crm.customers.customer_id")]
    f9 = results[("legacy.tb_trx02.f9", "inc:store.stores.store_id")]
    assert not f2["passed"] and f2["metric"] == 0.0
    assert not f9["passed"] and f9["metric"] == 0.0
    assert not any("failed" in n for n in report.notes), report.notes
    assert report.hints["refuted"] == 2
    tight = _settings(max_pages_per_table=1)
    async with factory() as s:
        refused = await run_stage3(tight, s, ProfileOptions(
            target="demo_pg", tables=["legacy.xx_aux"]))
    assert [t["method"] for t in refused.tests] == ["not run"]
    async with factory() as s:
        again = await run_stage3(tight, s, ProfileOptions(target="demo_pg", tables=scope,
                                                           dry_run=True))
        stored = (await s.scalars(sa.select(VerificationTest).where(
            VerificationTest.column_name == "id", VerificationTest.is_current.is_(True)))).one()
        snapshot = await _introspect(factory)
        evidence = await ProfileStore(s).load_evidence(ds, snapshot, enabled=True)
    assert not [r for r in again.results if r.qualified_name.endswith(("f2", "f9", ".id"))]
    assert stored.method == "not_run" and stored.metric is None
    (xx_test,) = evidence.table_evidence("legacy.xx_aux").tests["id"]
    assert xx_test.conclusive is False

    # 2. re-reasoning: the fake model proposes no join for tb_trx02, so a hint
    #    from "an earlier answer" becomes stale; refuted ones stay refuted
    async with factory() as s:
        s.add(JoinHint(data_source_id=ds, from_schema="legacy", from_table="tb_trx02",
                       from_columns=["f10"], to_schema="finance", to_table="currencies",
                       to_columns=["currency_code"], source="llm", status="inferred",
                       confidence=0.6))
        await s.execute(sa.update(ColumnSemantic).where(ColumnSemantic.is_current.is_(True))
                        .values(prompt_version="older"))  # force one re-reasoning
        await s.commit()
    await reason()
    async with factory() as s:
        status = {(h.from_columns[0], h.to_columns[0]): h.status for h in (await s.scalars(
            sa.select(JoinHint).where(JoinHint.source == "llm"))).all()}
    assert status[("f2", "customer_id")] == "refuted"
    assert status[("f9", "store_id")] == "refuted"
    assert status[("f10", "currency_code")] == "stale"


@pytest.mark.asyncio
async def test_views_inherit_and_rescoring_needs_no_model(
    pg_session_factory, fake_llm, fake_embedder
):
    """Owner decisions 2026-11-15: pass-through view columns take their base
    column's meaning without an LLM call (1, 3); stored answers are re-scored
    without the model when only what the scorer reads has changed (1, D9)."""
    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    scope = ["legacy.tb_trx02", "rpt.v_legacy_transactions"]
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg", tables=scope))

    async def reason(cfg=settings, **kw):
        async with factory() as s:
            return await run_reasoning(cfg, s, ReasoningOptions(target="demo_pg", tables=scope,
                                                                **kw),
                                       llm_factory=lambda: fake_llm, embedder=fake_embedder)

    first = await reason()
    assert first.count("reasoned") == 2
    view_requests = [m for m in fake_llm.requests
                     if '"table": "rpt.v_legacy_transactions"' in m[1].content]
    assert view_requests  # the computed column still goes to the model ...
    for messages in view_requests:  # ... alone: the pass-through columns are context
        asked = re.search(r'"columns": \[(.*?)\]', messages[1].content, re.S).group(1)
        assert '"transaction_date"' in asked and '"customer_code"' not in asked

    async with factory() as s:
        rows = (await s.execute(
            sa.select(ColumnRecord.column_name, TableRecord.object_name, ColumnSemantic)
            .join(ColumnSemantic, ColumnSemantic.column_id == ColumnRecord.id)
            .join(TableRecord, TableRecord.id == ColumnRecord.table_id)
            .where(ColumnSemantic.is_current.is_(True)))).all()
        queued = set((await s.scalars(sa.select(ColumnRecord.column_name).join(
            ReviewItem, ReviewItem.column_id == ColumnRecord.id).where(
            ReviewItem.status == "open"))).all())
    sem = {(t, c): s for c, t, s in rows}
    view_col, base_col = sem[("v_legacy_transactions", "customer_code")], sem[("tb_trx02", "f2")]
    assert view_col.score_breakdown["inherited_from"] == "legacy.tb_trx02.f2"
    assert (view_col.business_name, view_col.role, view_col.calibrated_score) == (
        base_col.business_name, base_col.role, base_col.calibrated_score)
    assert "customer_code" not in queued and "store_code" not in queued  # never queued
    assert "transaction_id" in {c for (t, c) in sem if t == "v_legacy_transactions"}

    # a hint an earlier answer proposed from a pass-through column goes stale on the
    # next version of the view: the join belongs to the base column
    async with factory() as s:
        ds = await s.scalar(sa.select(TableRecord.data_source_id).limit(1))
        s.add(JoinHint(data_source_id=ds, from_schema="rpt", from_table="v_legacy_transactions",
                       from_columns=["customer_code"], to_schema="crm", to_table="customers",
                       to_columns=["customer_id"], source="llm", status="inferred",
                       confidence=0.6))
        await s.commit()

    # nothing changed: nothing reasoned, nothing re-scored
    fake_llm.calls.clear()
    again = await reason()
    assert again.count("skipped") == 2 and sum(fake_llm.calls.values()) == 0

    # the scorer's inputs change (a strength), the model's do not: re-score only
    tuned = _settings()
    tuned.scoring.v2.strengths.view_alias = 0.55
    rescored = await reason(tuned)
    assert rescored.count("rescored") == 2 and sum(fake_llm.calls.values()) == 0
    async with factory() as s:
        versions = dict((await s.execute(
            sa.select(TableRecord.object_name, TableSemantic.version)
            .join(TableSemantic, TableSemantic.table_id == TableRecord.id)
            .where(TableSemantic.is_current.is_(True)))).all())
    assert versions == {"tb_trx02": 2, "v_legacy_transactions": 2}
    async with factory() as s:
        hint = await s.scalar(sa.select(JoinHint.status).where(
            JoinHint.from_table == "v_legacy_transactions"))
    assert hint == "stale"

    # evidence the model would see changes (a new claim test): `make reason` would
    # re-reason; `make rescore` scores the stored answer against it, no model call,
    # and keeps the answer's evidence hash so a later `make reason` still re-reasons
    async with factory() as s:
        ds = await s.scalar(sa.select(TableRecord.data_source_id).limit(1))
        trx = await s.scalar(sa.select(TableRecord.id).where(TableRecord.object_name ==
                                                               "tb_trx02"))
        s.add(VerificationTest(data_source_id=ds, table_id=trx, column_name="f1",
                               test_id="uniq:f1", kind="uniqueness", stage=3, origin="claim",
                               specific=True, passed=True, metric=1.0, detail="200k of 200k",
                               method="exact", runtime_ms=1, is_current=True))
        await s.commit()
    only = await reason(tuned, rescore_only=True)
    assert {r.qualified_name: r.action for r in only.results}["legacy.tb_trx02"] == "rescored"
    assert sum(fake_llm.calls.values()) == 0
    plan = await reason(tuned, dry_run=True)
    assert {r.qualified_name: (r.action, r.detail) for r in plan.results}[
        "legacy.tb_trx02"] == ("would_reason", "evidence changed")


@pytest.mark.asyncio
async def test_join_candidates_reach_tables_profiled_in_an_earlier_run(pg_session_factory):
    """Owner run 2026-11-15 (D12): the Phase 2 scope was profiled before its
    parents were, and later runs never paired its columns with them."""
    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        early = await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["legacy.tb_trx02"]))
    assert not any(j["child"] == "legacy.tb_trx02.f2" for j in early.joins)  # no parent yet
    async with factory() as s:
        later = await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["crm.customers", "store.stores"]))
    found = {(j["child"], j["parent"]): j for j in later.joins}
    f2 = found[("legacy.tb_trx02.f2", "crm.customers.customer_code")]
    assert f2["passed"] and f2["kept"]
    assert ("legacy.tb_trx02.f9", "store.stores.store_code") in found
    assert later.join_samples == 1  # one sample statement for the earlier table
    assert any("re-sampled" in n for n in later.notes)
    async with factory() as s:
        again = await run_profiling(settings, s, ProfileOptions(
            target="demo_pg", tables=["crm.customers", "store.stores"], force=True))
    assert not any(j["child"].startswith("legacy.") for j in again.joins)  # already tested
    assert again.join_samples == 0  # and not even sampled again
