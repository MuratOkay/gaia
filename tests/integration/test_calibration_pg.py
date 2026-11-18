"""Calibrators in the store and in the scores (6C, D86), and the owner's 3C audit as labels
(6B part 2, D85), on ``demo_pg``.

* a calibrator put in use re-scales the stored meanings on ``make rescore`` — P(right) instead of
  the raw score, P6-3's band, the calibrator's version beside the score, the review queue
  re-ranked — and going back to the identity restores them; while none is in use the stored
  scores' inputs are unchanged, so no data source is re-scored for nothing;
* ``make calibrate`` fits on labels, refuses to apply below P6-1 or when the identity is as good,
  and applies one that beats it;
* the 3C audit is imported once, with the judge's verdict beside each label; only production-mode
  conditions count for calibration; the judge's agreement (κ) is reported.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random

import pytest
import sqlalchemy as sa

from gaia.db.models.evaluation import Calibrator, Label
from gaia.db.models.semantics import ColumnSemantic, ReviewItem
from gaia.evaluation.calibration import Fit
from gaia.evaluation.calibrators import in_use, meaning_points, put_in_use
from gaia.evaluation.human_labels import (
    all_labels,
    import_3c_audit,
    judge_agreement,
    read_3c_audit,
)
from gaia.semantics.service import ReasoningOptions, run_reasoning

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
DB = os.environ.get("GAIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")


async def _rescore(factory, settings, fake_llm, fake_embedder):
    async with factory() as s:
        return await run_reasoning(settings, s, ReasoningOptions(target="demo_pg",
                                                                 rescore_only=True),
                                   llm_factory=lambda: fake_llm, embedder=fake_embedder)


async def _current(factory, table: str, column: str) -> ColumnSemantic:
    from test_review_pg import _col
    from test_review_pg import _current as current

    return await current(factory, (await _col(factory, table, column)).id)


@pytest.mark.asyncio
async def test_a_calibrator_in_use_rescales_the_stored_scores(pg_session_factory, fake_llm,
                                                               fake_embedder):
    from test_review_pg import _world

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    before = await _current(factory, "sales.orders", "total_amount")
    assert before.calibrator == "identity" and before.calibrated_score == before.raw_score

    # no calibrator in use: nothing to re-score (the stored inputs did not move)
    report = await _rescore(factory, settings, fake_llm, fake_embedder)
    assert report.count("rescored") == 0
    async with factory() as s:
        open_before = await s.scalar(sa.select(sa.func.count()).select_from(ReviewItem).where(
            ReviewItem.status == "open"))

    # a calibrator saying every meaning of this model is right 97 % of the time, on 400 labels
    async with factory() as s:
        row = Calibrator(version="isotonic-test000001", kind="meaning", model=before.model,
                         scoring="heuristic-v2", method="isotonic",
                         params={"blocks": [[0.0, 1.0, 0.97, 400]]}, metrics={}, labels=400,
                         labels_wrong=40, labels_key="k", in_use=False)
        s.add(row)
        await s.flush()
        await put_in_use(s, row, kind="meaning", model=before.model, scoring="heuristic-v2")
        await s.commit()
        assert (await in_use(s, "meaning"))[(before.model, "heuristic-v2")].p(0.1) == 0.97
    report = await _rescore(factory, settings, fake_llm, fake_embedder)
    assert report.count("rescored") > 0
    after = await _current(factory, "sales.orders", "total_amount")
    assert after.version > before.version and after.raw_score == before.raw_score
    assert after.calibrated_score == 0.97 and after.calibrator == "isotonic-test000001"
    assert after.band == "accepted"
    assert after.score_breakdown["calibrated_lower"] >= 0.8
    async with factory() as s:
        open_after = await s.scalar(sa.select(sa.func.count()).select_from(ReviewItem).where(
            ReviewItem.status == "open"))
    assert open_after < open_before  # accepted meanings leave the queue

    # back to the identity
    async with factory() as s:
        await put_in_use(s, None, kind="meaning", model=before.model, scoring="heuristic-v2")
        await s.commit()
    await _rescore(factory, settings, fake_llm, fake_embedder)
    back = await _current(factory, "sales.orders", "total_amount")
    assert back.calibrator == "identity" and back.calibrated_score == back.raw_score
    assert back.band == before.band


@pytest.mark.asyncio
async def test_calibrate_fits_refuses_and_applies(pg_session_factory, monkeypatch, capsys):
    from gaia.datasources.resolver import clear_cache
    from gaia.db.engine import dispose_engine
    from gaia.tools import calibrate

    monkeypatch.setenv("GAIA_METADATA_URL", DB)
    clear_cache()
    await dispose_engine()  # the tool's engine must be the test store's
    factory = pg_session_factory
    rng = random.Random(11)

    def labels(n, model, truth):
        out = []
        for i in range(n):
            score = round(rng.random(), 4)
            out.append(Label(source="3c-audit", origin=f"{model}|{i}", data_source="demo_pg",
                             subject=f"s.t.c{i}", verdict="right" if rng.random() < truth(score)
                             else "wrong", production=True, model=model,
                             scoring="heuristic-v2", raw_score=score))
        return out

    async with factory() as s:
        s.add_all(labels(20, "few-model", lambda x: x))          # below P6-1
        s.add_all(labels(400, "honest-model", lambda x: x))      # already calibrated
        s.add_all(labels(400, "bold-model", lambda x: x * x))    # over-confident
        await s.commit()

    def args(**kw):
        base = {"kind": "meaning", "model": None, "method": None, "apply": False,
                "force": False, "reset": False, "list": False}
        return argparse.Namespace(**(base | kw))

    assert await calibrate._run(args()) == 0  # fits and judges, changes nothing
    out = capsys.readouterr().out
    assert "too few to calibrate" in out and "beats the identity on both" in out
    assert "does **not** beat the identity" in out
    async with factory() as s:
        assert await in_use(s, "meaning") == {}
    assert await calibrate._run(args(model="few-model", apply=True)) == 1
    assert "nothing to apply" in capsys.readouterr().out
    assert await calibrate._run(args(model="honest-model", apply=True)) == 1
    assert "not applied" in capsys.readouterr().out
    assert await calibrate._run(args(model="bold-model", apply=True)) == 0
    out = capsys.readouterr().out
    assert "→ in use: isotonic-" in out
    async with factory() as s:
        used = await in_use(s, "meaning")
        assert list(used) == [("bold-model", "heuristic-v2")]
        fit: Fit = used[("bold-model", "heuristic-v2")]
        assert fit.p(0.5) < 0.4  # 0.5 meant right a quarter of the time
        kept = (await s.scalars(sa.select(Calibrator))).all()
        assert {r.model for r in kept} == {"honest-model", "bold-model"}  # the identity: never
    assert await calibrate._run(args(list=True)) == 0
    assert "IN USE" in capsys.readouterr().out
    assert await calibrate._run(args(model="bold-model", reset=True)) == 0
    async with factory() as s:
        assert await in_use(s, "meaning") == {}


def _write_3c(tmp_path):
    out = tmp_path / "nge_like_3c"
    for code in ("N-P", "B-P"):
        (out / code).mkdir(parents=True)
        cols = [{"table": "dbo.orders", "column": c, "score": sc, "weights_version": None}
                for c, sc in (("amount", 0.82), ("status", 0.41), ("cust", 0.66))]
        (out / code / "results.json").write_text(json.dumps({
            "summary": {"calls": [{"purpose": "semantics", "model": "deepseek-x"},
                                  {"purpose": "critique", "model": "other"}]},
            "columns": cols}), encoding="utf-8")
        (out / code / "judgements.json").write_text(json.dumps({"verdicts": {
            "dbo.orders.amount": {"meaning": "yes"}, "dbo.orders.status": {"meaning": "yes"},
            "dbo.orders.cust": {"meaning": "partial"}}}), encoding="utf-8")
    (out / "plan.json").write_text(json.dumps({"data_source": "nge_like",
                                               "conditions": ["N-P", "B-P"]}),
                                   encoding="utf-8")
    rows = [("N-P", "amount", "yes"), ("N-P", "status", "no"), ("B-P", "cust", "partial"),
            ("B-P", "amount", ""), ("N-P", "gone", "yes")]
    with open(out / "audit.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["condition", "table", "column", "owner_meaning",
                                           "owner_note"])
        w.writeheader()
        for code, col, verdict in rows:
            w.writerow({"condition": code, "table": "dbo.orders", "column": col,
                        "owner_meaning": verdict, "owner_note": ""})
    return out


@pytest.mark.asyncio
async def test_the_owners_3c_audit_becomes_labels(pg_session_factory, tmp_path):
    factory = pg_session_factory
    out = _write_3c(tmp_path)
    files = read_3c_audit(out)
    async with factory() as s:
        with pytest.raises(ValueError, match="not 'demo_pg'"):
            await import_3c_audit(s, files, data_source="demo_pg")
        r = await import_3c_audit(s, files, created_by="tester")
        await s.commit()
    assert (r["imported"], r["known"], r["unfilled"]) == (3, 0, 1)
    assert r["missing"] == ["N-P dbo.orders.gone"]
    async with factory() as s:
        again = await import_3c_audit(s, read_3c_audit(out))
        await s.commit()
        assert (again["imported"], again["known"]) == (0, 3)
        labels = await all_labels(s, "nge_like")
    got = {(x.prompt_version, x.column): (x.verdict, x.judge, x.production, x.raw_score)
           for x in labels}
    assert got == {("N-P", "dbo.orders.amount"): ("right", "right", True, 0.82),
                   ("N-P", "dbo.orders.status"): ("wrong", "right", True, 0.41),
                   ("B-P", "dbo.orders.cust"): ("partly", "partly", False, 0.66)}
    assert {x.model for x in labels} == {"deepseek-x"} and all(x.source == "3c-audit"
                                                               for x in labels)
    j = judge_agreement(labels)
    assert (j["n"], j["agree"], j["judge_more_lenient"], j["judge_stricter"]) == (3, 2, 1, 0)
    # only the production-mode audits are calibrated on
    points = meaning_points(labels)
    assert [len(v) for v in points.values()] == [2]


@pytest.mark.asyncio
async def test_regressions_are_no_labels_and_a_wrong_name_is_refused(pg_session_factory,
                                                                      monkeypatch, capsys):
    """D89 (OWNER_VERIFICATION §30): the host's three ``--regress joins-rule`` runs were read as
    answer labels (a prompt Gaia never answers anyone with), and ``make labels TARGET=nge_small``
    — the database's name, not the data source's — said "no label yet" instead of no."""
    from datetime import UTC, datetime

    from gaia.datasources.resolver import clear_cache
    from gaia.db.engine import dispose_engine
    from gaia.db.models.evaluation import EvaluationItem, EvaluationRun
    from gaia.evaluation.calibrators import ANSWER_SCORING, answer_points
    from gaia.tools import labels as labels_tool
    from gaia.tools import quality as quality_tool

    monkeypatch.setenv("GAIA_METADATA_URL", DB)
    clear_cache()
    await dispose_engine()
    factory = pg_session_factory
    async with factory() as s:
        for build, ok in (("D88", True), ("D88 + deliberate regression: joins-rule", False)):
            run = EvaluationRun(kind="golden", data_source="demo_pg", question_set_key="S",
                                build=build, model="m", mode="fresh",
                                started_at=datetime.now(UTC))
            s.add(run)
            await s.flush()
            s.add(EvaluationItem(run_id=run.id, item_key="g01", correct=ok,
                                 final_status="answered",
                                 detail={"confidence": {"score": 0.7, "level": "medium"}}))
        await s.commit()
        points = await answer_points(s)
    assert [(p.score, p.y) for p in points[("m", ANSWER_SCORING)]] == [(0.7, 1.0)]

    assert await labels_tool._run(argparse.Namespace(
        target="nge_small", import_3c=None, csv=None, audit=None, reviewer=None)) == 2
    err = capsys.readouterr().err
    assert "no data source 'nge_small'" in err and "demo_mssql" in err and "demo_pg" in err
    assert await quality_tool._run(argparse.Namespace(
        target="nge_small", last=3, runs=None, set=None, baseline=None, candidate=None,
        compare_builds=False, list=False, import_glob=None)) == 2
    assert "no data source 'nge_small'" in capsys.readouterr().err
    # a configured name with nothing yet still says so, as before
    assert await labels_tool._run(argparse.Namespace(
        target="demo_pg", import_3c=None, csv=None, audit=None, reviewer=None)) == 0
    assert "no label on demo_pg yet" in capsys.readouterr().out
