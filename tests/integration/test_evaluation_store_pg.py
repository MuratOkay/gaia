"""Phase 6A (D80) on ``demo_pg``: golden runs recorded in the evaluation store, replays told
apart from measurements, older runs imported, and the quality report read back."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from gaia.db.models.evaluation import EvaluationItem, EvaluationRun
from gaia.dialects.postgres.guard import PostgresGuard
from gaia.evaluation.quality import quality_report, select_runs
from gaia.evaluation.recorder import RecordingLLMClient
from gaia.evaluation.store import (
    import_golden_dir,
    load_runs,
    question_set_key,
    record_golden,
    run_mode,
)
from gaia.query.ask import AskSettings
from gaia.query.golden import load_golden
from gaia.query.golden_run import run_golden, summarise
from gaia.query.retrieval_store import load_retrieval_index

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
GOLDEN = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "golden.yaml"


@pytest.mark.asyncio
async def test_golden_runs_become_measurements(pg_session_factory, tmp_path):
    from test_golden_pg import GoldenModel, _plan
    from test_profiling_pg import _introspect, _settings, _target

    factory = pg_session_factory
    await _introspect(factory)
    settings = _settings()
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    golden = load_golden(GOLDEN)
    only = ["g01", "g02", "g03", "g05", "o01"]
    guard = PostgresGuard(index.catalog, row_limit=1000)
    ask = AskSettings(model="scripted", statement_timeout_s=30)

    async def golden_run(model, out: Path, build: str):
        llm = RecordingLLMClient(model, out / "llm")
        outcomes = run_golden(golden, index=index, guard=guard, target=_target(), llm=llm,
                              settings=ask, only=only, workers=2)
        async with factory() as s:
            run = await record_golden(
                s, data_source="demo_pg", outcomes=outcomes, summary=summarise(outcomes),
                question_set=str(GOLDEN), out_dir=str(out), model="scripted", build=build,
                set_key=question_set_key((o.id, o.question, o.expect) for o in outcomes),
                live_calls=llm.live_calls, replayed_calls=llm.replayed,
                started_at=datetime.now(UTC))
            await s.commit()
        return run, outcomes

    right = GoldenModel(golden)
    first, outs = await golden_run(right, tmp_path / "a", "B1")
    assert first.mode == "fresh" and first.live_calls > 0
    second, _ = await golden_run(right, tmp_path / "b", "B1")
    # the same directory again: every answer replayed — recorded as what it is
    again, _ = await golden_run(right, tmp_path / "a", "B1")
    assert again.mode == "replay" and again.live_calls == 0 and run_mode(3, 2) == "mixed"

    # a build whose model gets g01 wrong
    wrong = GoldenModel(golden)
    g01 = next(q for q in golden.questions if q.id == "g01")
    wrong.first[g01.question] = _plan(sql=g01.sql.replace("AND status <> 'cancelled'", ""))
    third, _ = await golden_run(wrong, tmp_path / "c", "B2")
    fourth, _ = await golden_run(wrong, tmp_path / "d", "B2")

    async with factory() as s:
        items = (await s.scalars(sa.select(EvaluationItem).where(
            EvaluationItem.run_id == first.id))).all()
        assert sorted(i.item_key for i in items) == sorted(only)
        assert all(i.correct for i in items if i.expect == "answer")
        # an older run's files, imported once
        older = tmp_path / "older"
        older.mkdir()
        (older / "results.json").write_text(json.dumps({
            "data_source": "demo_pg", "model": "deepseek-flash", "build": "D75", "summary": {},
            "outcomes": [{k: v for k, v in o.__dict__.items() if k != "answers"}
                         for o in outs]}, default=str))
        past = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
        os.utime(older / "results.json", (past, past))  # its run is older than the others
        imported = await import_golden_dir(s, older, created_by="test")
        assert imported.mode == "imported" and imported.question_set_key == first.question_set_key
        assert await import_golden_dir(s, older) is None  # not twice
        await s.commit()
        assert await s.scalar(sa.select(sa.func.count()).select_from(EvaluationRun)) == 6
        runs = await load_runs(s, "demo_pg")

    assert runs[0].build == "D75" and runs[0].mode == "imported"
    sel = select_runs(runs, compare_builds=True)
    assert [r.build for r in sel.candidate] == ["B2", "B2"]
    assert len(sel.baseline) == 2 and all(r.build == "B1" for r in sel.baseline)
    assert [r.mode for r in sel.excluded] == ["replay"]
    text, doc = quality_report(sel.shown, data_source="demo_pg", question_set_key=sel.set_key,
                               excluded=sel.excluded, baseline=sel.baseline,
                               candidate=sel.candidate)
    assert doc["comparison"]["moved"] == [["g01", 1.0, 0.0]]
    assert "g01: right in 100% of the baseline's runs, 0% of the candidate's" in text
