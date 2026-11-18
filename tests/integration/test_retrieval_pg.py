"""Phase 4B on ``demo_pg``: the retrieval index from the real store, pgvector
search, contexts, and the guard agreeing with every join a context offers.

Semantics come from the conftest's fake model (meanings derived from names) and
embeddings from its fake embedder (hash vectors): this is a functional test of
the machinery, not of retrieval quality — that needs real meanings and bge-m3
(OWNER_VERIFICATION §16).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from gaia.db.models.semantics import ColumnSemantic, Embedding
from gaia.dialects.postgres.guard import PostgresGuard
from gaia.profiling.service import ProfileOptions, run_profiling
from gaia.query.executor import execute
from gaia.query.retrieval import retrieve
from gaia.query.retrieval_store import load_retrieval_index, vector_search
from gaia.semantics.service import ReasoningOptions, run_reasoning

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
QUESTIONS = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / \
    "retrieval_questions.csv"


@pytest.mark.asyncio
async def test_retrieval_on_demo_pg(pg_session_factory, fake_llm, fake_embedder):
    from test_profiling_pg import _introspect, _settings, _target

    factory = pg_session_factory
    snapshot = await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg"))
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg"),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    # one meaning abstains: it must not be used (R2), the column keeps its name
    async with factory() as s:
        await s.execute(sa.text(
            "UPDATE semantics.column_semantic c SET band = 'abstain' FROM catalog.columns k, "
            "catalog.tables t WHERE k.id = c.column_id AND t.id = c.table_id AND c.is_current "
            "AND t.object_name = 'customers' AND k.column_name = 'loyalty_points'"))
        await s.commit()

    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    assert len(index.objects) == len(snapshot.tables) == 28
    customers = index.objects["crm.customers"]
    assert customers.business_name and customers.column("segment").business_name
    assert customers.column("loyalty_points").business_name is None  # abstained
    # exposure (D24): personal data shows no values; enums and small code sets do
    assert customers.column("email").values == []
    assert customers.column("phone").values == []
    assert customers.column("segment").values == ["retail", "smb", "corporate", "vip"]
    assert "web" in index.objects["sales.orders"].column("channel").values
    assert index.objects["sales.orders"].column("order_date").range is not None
    assert index.objects["legacy.tb_trx02"].column("f3").semantic_type == "date_int"

    # pgvector: the exact text of a stored column meaning is its own nearest neighbour,
    # and only current versions come back
    async with factory() as s:
        row = (await s.execute(sa.select(Embedding.content, Embedding.column_id)
                               .join(ColumnSemantic,
                                     ColumnSemantic.id == Embedding.column_semantic_id)
                               .where(ColumnSemantic.is_current.is_(True),
                                      ColumnSemantic.band != "abstain")
                               .limit(1))).one()
        vector = fake_embedder.embed([row.content]).vectors[0]
        hits = await vector_search(s, index, vector, "bge-m3", k=10)
        column_name = await s.scalar(sa.text(
            "SELECT column_name FROM catalog.columns WHERE id = :i"), {"i": row.column_id})
        abstained = await vector_search(s, index, fake_embedder.embed(["x"]).vectors[0],
                                        "bge-m3", k=500)
    assert hits[0].column == column_name and hits[0].similarity > 0.999
    assert ("crm.customers", "loyalty_points") not in {(h.obj, h.column) for h in abstained}
    assert not await _none(factory, index)  # another model's vectors are not mixed in

    # contexts: the right objects for plain questions, and every join they offer is one
    # the guard accepts and the database runs
    guard = PostgresGuard(index.catalog, row_limit=10)
    target = _target()
    for question, expected in [
        ("total amount of orders by store city", {"sales.orders", "store.stores"}),
        ("quantity of order lines per product category", {"sales.order_lines",
                                                          "product.categories"}),
        ("employees per department", {"hr.employees", "hr.departments"}),
    ]:
        ctx = retrieve(index, question, vector_hits=hits)
        assert expected <= set(ctx.tables), (question, ctx.tables)
        assert ctx.render().startswith("Database: demo_pg (PostgreSQL)")
        for e in ctx.joins:
            sql = (f"SELECT count(*) FROM {e.from_table} a JOIN {e.to_table} b "  # noqa: S608
                   f"ON a.{e.from_column} = b.{e.to_column}")
            g = guard.check(sql)
            assert g.ok, (question, sql, [str(v) for v in g.errors])
            assert execute(target, g, row_limit=10).ok

    # the evaluation tool over the demo_pg question set (names-only meanings here)
    from gaia.tools.retrieve import _load_questions, evaluate

    items = _load_questions(QUESTIONS)
    assert len(items) >= 20
    # a clarified question is searched with its answer (7B, D91)
    from gaia.tools.retrieve import searched

    contexts = [retrieve(index, searched(q, c)) for q, _e, c in items]
    report = evaluate(contexts, [e for _q, e, _c in items], 5)
    assert report["questions"] == len(items) and 0 <= report["mrr"] <= 1
    print(f"\nnames-only baseline (fake meanings, no vectors): covered {report['covered']:.0%}, "
          f"connected {report['connected']:.0%}, recall@5 {report['recall_at_k']:.0%}, "
          f"MRR {report['mrr']:.2f}, {report['mean_objects']:.1f} objects per context")
    for p in report["per_question"]:
        flag = "ok  " if p["connected"] else ("part" if p["covered"] else "MISS")
        print(f"  {flag} {p['question'][:60]:<60} {p['size']} objects")


async def _none(factory, index) -> list:
    async with factory() as s:
        return await vector_search(s, index, [0.0] * 1023 + [1.0], "another-model", k=5)
