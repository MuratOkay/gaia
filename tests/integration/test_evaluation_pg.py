"""Phase 3C harness end to end on ``demo_pg`` with a fake model and judge.

What must hold whatever the model says: name-blind prompts carry no original
identifier; hidden objects are gone; nothing is written to the metadata store's
semantics; every answer is recorded, so a re-run makes no call; judging,
auditing and the report work from the run directory alone.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from gaia.db.models.semantics import ColumnSemantic, TableSemantic
from gaia.db.repositories.profile_store import ProfileStore
from gaia.dialects.postgres.profiler import PostgresProfiler
from gaia.evaluation.labels import load_labels
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.evaluation.report import build_report, judge_condition, write_audit
from gaia.evaluation.runner import EvalContext, run_condition
from gaia.llm.base import LLMResponse, Usage
from gaia.profiling.service import ProfileOptions, run_profiling

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
LABELS = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "labels.csv"
SCOPE = ["legacy.tb_mst01", "legacy.tb_prm03", "legacy.tb_trx02", "legacy.xx_aux"]


class EvalFake:
    """The conftest's fake reasoner plus a fake judge: 'yes' when the answer's
    name shares a word with the reference, else 'no'."""

    def __init__(self, fake_llm):
        self.fake = fake_llm
        self.user_messages: list[tuple[str, str]] = []  # (purpose, content)

    def complete(self, messages, **kw):
        system = messages[0].content
        if system.startswith("You are judging"):
            doc = json.loads(messages[1].content.split("\n", 1)[1])
            cols = []
            for c in doc["columns"]:
                ref = json.dumps(c["reference"]).lower()
                words = {w for w in c["answer"].get("business_name", "").lower().split()
                         if len(w) > 2}
                cols.append({"column": c["column"],
                             "meaning": "yes" if any(w in ref for w in words) else "no",
                             "role_ok": None, "note": "fake"})
            self.user_messages.append(("judge", messages[1].content))
            return LLMResponse(content=json.dumps({"columns": cols}), model="fake",
                               finish_reason="stop", usage=Usage(prompt_tokens=10))
        self.user_messages.append(("reason", messages[1].content))
        return self.fake.complete(messages, **kw)


@pytest.mark.asyncio
async def test_evaluation_harness_end_to_end(pg_session_factory, fake_llm, tmp_path):
    from test_profiling_pg import _introspect, _settings, _target

    factory = pg_session_factory
    snapshot = await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg", tables=[
            *SCOPE, "crm.customers", "store.stores", "finance.currencies"]))
        ds = await s.scalar(sa.text("SELECT id FROM catalog.data_sources LIMIT 1"))
        evidence = await ProfileStore(s).load_evidence(ds, snapshot, enabled=True)

    fake = EvalFake(fake_llm)
    llm = RecordingLLMClient(fake, tmp_path / "llm")
    ctx = EvalContext(
        settings=settings, data_source="demo_pg", snapshot=snapshot, scope=SCOPE,
        evidence=evidence, llm=llm, out=tmp_path, hide=["rpt.v_legacy_transactions"],
        profiler_factory=lambda: PostgresProfiler(_target(), settings.profiling.budget),
    )
    for code in ("N-C", "N-P", "B-C", "B-P", "N-P-h"):
        before = len(fake.user_messages)
        summary = run_condition(ctx, code)
        assert summary["columns"] == 24 and not summary["failed"], summary
        prompts = [m for _p, m in fake.user_messages[before:]]
        joined = "\n".join(prompts)
        if code.startswith("B-"):
            for name in ("tb_trx02", "tb_mst01", "legacy.", "customer_code", "store_code",
                         "v_legacy_transactions", "crm.customers", "gaia_target_pg"):
                assert name not in joined, (code, name)
        if code == "N-P":
            assert "v_legacy_transactions" in joined and "transaction_date" in joined
        if code == "N-P-h":
            assert "v_legacy_transactions" not in joined and "transaction_date" not in joined

    # results carry the real names, whatever the model saw
    blind = json.loads((tmp_path / "B-P" / "results.json").read_text())
    assert {r["table"] for r in blind["columns"]} == set(SCOPE)
    assert all(r["world_table"] != r["table"] for r in blind["columns"])

    # nothing written to the semantics of the metadata store
    async with factory() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(TableSemantic)) == 0
        assert await s.scalar(sa.select(sa.func.count()).select_from(ColumnSemantic)) == 0

    # resumable: every answer comes from the recording
    live = llm.live_calls
    run_condition(ctx, "N-C")
    assert llm.live_calls == live

    labels = load_labels(LABELS)
    for code in ("N-C", "N-P", "B-C", "B-P", "N-P-h"):
        judged = judge_condition(settings, llm, tmp_path, code, "demo_pg", labels)
        assert judged["judged"] == 24 and not judged["errors"]
    n = write_audit(tmp_path, ["N-C", "B-P"], tmp_path / "audit.csv")
    assert n == round(48 * 0.2)
    rows = list(csv.DictReader((tmp_path / "audit.csv").read_text(encoding="utf-8")
                               .splitlines()))
    assert "judge" not in ",".join(rows[0])  # the owner does not see the verdict
    for row in rows:
        row["owner_meaning"] = "yes"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (tmp_path / "audit.csv").write_text(buffer.getvalue(), encoding="utf-8")
    text, doc = build_report(tmp_path, "demo_pg", labels, 0.8, tmp_path / "audit.csv")
    print(text)
    assert "| B-P beats B-C by ≥ 15 points (all columns) |" in text
    assert doc["conditions"]["N-P"]["labelled"]["n"] == 24  # ground truth for every column
    audit = next(c for c in doc["criteria"] if c["name"].startswith("owner audit"))
    assert "audited" in audit["value"]
