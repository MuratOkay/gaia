"""The replayed regression net (Phase 6E, D82) on ``demo_pg``: every golden question through the
whole pipeline with a scripted model, its prompts fingerprinted and its outcome kept, against the
stored net ``tests/fixtures/golden_net/demo_pg.json``. A change that moves a prompt or an outcome
fails here and names the question; ``make golden-net-accept`` accepts an intended one.

The scripted model errs on purpose, so the net watches the guard, the repairs and the scorer too:
g03 first joins order lines to payments (a fan-out the guard must refuse), g04 first names a
column that does not exist (repaired), g01 keeps cancelled orders (scored wrong), o06's best
guess is wrong."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gaia.evaluation.net import FingerprintingLLM, differences, snapshot
from gaia.query.ask import AskSettings
from gaia.query.golden import load_golden
from gaia.query.golden_run import run_golden
from gaia.query.retrieval_store import load_retrieval_index

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "infra" / "pg-target" / "golden.yaml"
# the stored net; a server whose data profiles differently keeps its own (GAIA_GOLDEN_NET)
NET = Path(os.environ.get("GAIA_GOLDEN_NET")
           or ROOT / "tests" / "fixtures" / "golden_net" / "demo_pg.json")
NET = NET if NET.is_absolute() else ROOT / NET


def _net_model(golden):
    from test_golden_pg import GoldenModel, _plan

    class NetModel(GoldenModel):
        """GoldenModel, answering repairs with the reference, and wrong on purpose where the
        docstring says."""

        def __init__(self, golden):
            super().__init__(golden)
            self.repair: dict[str, str] = {}

        def complete(self, messages, **kw):
            if len(messages) > 2 and not messages[0].content.startswith("You explain"):
                question = messages[1].content.rsplit("Question: ", 1)[-1].strip()
                if question in self.repair:
                    from gaia.llm.base import LLMResponse, Usage

                    return LLMResponse(content=self.repair[question], model="scripted",
                                       finish_reason="stop",
                                       usage=Usage(prompt_tokens=10, completion_tokens=5))
            return super().complete(messages, **kw)

    model = NetModel(golden)
    by = {q.id: q for q in golden.questions}
    g01, g03, g04 = by["g01"], by["g03"], by["g04"]
    model.first[g01.question] = _plan(sql=g01.sql.replace("AND status <> 'cancelled'", ""))
    model.first[g03.question] = _plan(sql=(
        "SELECT p.sku, p.name, p.product_id, sum(l.quantity) AS quantity "
        "FROM sales.order_lines l JOIN sales.payments pay ON pay.order_id = l.order_id "
        "JOIN sales.orders o ON o.order_id = l.order_id "
        "JOIN product.products p ON p.product_id = l.product_id "
        "WHERE o.order_date >= DATE '2025-01-01' AND o.order_date < DATE '2026-01-01' "
        "GROUP BY p.sku, p.name, p.product_id ORDER BY quantity DESC, p.sku LIMIT 10"))
    model.repair[g03.question] = _plan(sql=g03.sql)
    model.first[g04.question] = _plan(sql=g04.sql.replace("is_active", "is_active_flag"))
    model.repair[g04.question] = _plan(sql=g04.sql)
    o06 = by["o06"]
    guess = json.loads(model.first[o06.question])
    guess["sql"] = ("SELECT p.sku, p.name, (p.list_price - p.unit_cost) AS profit "
                    "FROM product.products p ORDER BY profit DESC LIMIT 5")
    model.first[o06.question] = json.dumps(guess)
    return model


async def _run(factory, settings, only=None):
    from gaia.dialects.postgres.guard import PostgresGuard
    from test_profiling_pg import _target

    golden = load_golden(GOLDEN)
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    llm = FingerprintingLLM(_net_model(golden))
    outcomes = run_golden(golden, index=index, guard=PostgresGuard(index.catalog, row_limit=1000),
                          target=_target(), llm=llm, settings=AskSettings(
                              model="scripted", statement_timeout_s=30), only=only, workers=3)
    return snapshot(outcomes, llm)


@pytest.mark.asyncio
async def test_the_replayed_net(pg_session_factory, fake_llm, fake_embedder, monkeypatch):
    from test_review_pg import _world

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    async with factory() as s:  # the legacy joins g16 needs, as test_golden_pg gives them
        import sqlalchemy as sa

        from gaia.db.models.semantics import JoinHint

        ds = await s.scalar(sa.text("SELECT id FROM catalog.data_sources LIMIT 1"))
        for col, schema, table, to_col in [("f2", "crm", "customers", "customer_code"),
                                           ("f9", "store", "stores", "store_code")]:
            s.add(JoinHint(data_source_id=ds, from_schema="legacy", from_table="tb_trx02",
                           from_columns=[col], to_schema=schema, to_table=table,
                           to_columns=[to_col], source="llm", status="inferred"))
        await s.commit()
    got = await _run(factory, settings)
    # the deliberate mistakes are where the docstring says
    assert got["g03"]["outcome"]["repairs"] == 1 and got["g03"]["outcome"]["correct"] is True
    assert [p["purpose"] for p in got["g03"]["prompts"]] == ["generation", "repair", "narration"]
    assert got["g04"]["outcome"]["repairs"] == 1
    assert got["g01"]["outcome"]["correct"] is False
    assert got["o06"]["outcome"]["best_guess"] == "wrong"
    if os.environ.get("GAIA_ACCEPT_NET"):
        NET.parent.mkdir(parents=True, exist_ok=True)
        NET.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        pytest.skip(f"the net accepted: {NET}")
    assert NET.exists(), "no stored net: make golden-net-accept"
    expected = json.loads(NET.read_text(encoding="utf-8"))
    moved = differences(expected, got)
    assert moved == [], ("the golden net moved — if intended, `make golden-net-accept` and "
                         "justify a prompt change with fresh golden runs (P6-2):\n"
                         + "\n".join(moved))

    # and it catches what it is for: a guard that lets a fan-out join through …
    import gaia.query.validator as validator

    monkeypatch.setattr(validator.JoinCheck, "verified", property(lambda self: True))
    broken = await _run(factory, settings, only=["g03"])
    moved = differences({"g03": expected["g03"]}, broken)
    assert any("g03: the outcome changed" in m and "repairs 1 → 0" in m for m in moved), moved
    monkeypatch.undo()

    # … and a prompt whose rule about joins was removed
    import gaia.query.planner as planner

    real = planner.query_prompt

    def without_joins_rule(name, major):
        p = real(name, major)
        return type(p)(**{**p.__dict__,
                          "text": p.text.replace("Join only as listed under JOINS.", "")})

    monkeypatch.setattr(planner, "query_prompt", without_joins_rule)
    changed = await _run(factory, settings, only=["g02"])
    moved = differences({"g02": expected["g02"]}, changed)
    assert moved and moved[0].startswith("g02: the prompt changed — generation (call 1)"), moved
