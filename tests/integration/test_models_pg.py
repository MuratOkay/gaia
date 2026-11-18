"""A change of model as a managed event (Phase 7D, D101) on ``demo_pg`` with scripted models.

A model is registered beside the configured one (its key encrypted, or named), probed, and
refused to serve until it passes; a switch needs a recent trial — or is forced, recorded —
and is rolled back while the rollback period runs; a model serving, or still a rollback
target, cannot be retired. The ``model_trial`` job runs the golden set interleaved with both
models and names the question the candidate breaks; re-reasons the tables with SME decisions
in a shadow (writing nothing) and judges both models' meanings by the same rule; and says what
a switch would cost. After a switch the nightly refresh re-reasons with the new model, the
SME's decisions kept; the candidate's golden runs stay out of the ordinary report."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from gaia.db.models.ops import Alert, Job, Model, ModelAssignment
from gaia.jobs.kinds import JobDeps
from gaia.jobs.queue import enqueue
from gaia.jobs.worker import Worker
from gaia.llm.base import LLMResponse, Usage
from gaia.models.registry import (
    ModelError,
    add_model,
    effective_settings,
    get_model,
    list_models,
    meanings_by_model,
    probe_model,
    retire,
    rollback,
    switch,
)
from gaia.models.trial import trial_lines

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")

CRITIC = "You are a sceptical reviewer"


class ProbeOK:
    def complete(self, messages, *, json_mode=False, thinking=False, **_kw):
        content = (json.dumps({"business_name": "Order total", "purpose": "p",
                               "confidence": 0.9}) if json_mode else "ready")
        return LLMResponse(content=content, model="qwen-max", finish_reason="stop",
                           usage=Usage(prompt_tokens=3, completion_tokens=3, total_tokens=6),
                           reasoning="thought" if thinking else None)


class ProbeNoJson(ProbeOK):
    def complete(self, messages, *, json_mode=False, **kw):
        r = super().complete(messages, json_mode=json_mode, **kw)
        if json_mode:
            r.content = "Here you are: business_name = Order total"
        return r


class Renaming:
    """The candidate in the reasoning trial: the same roles as the scripted model, but every
    column called "Field" — half-right by the rule (the role is; the name is not)."""

    def __init__(self, inner, model="qwen-max"):
        self.inner, self.model = inner, model

    def complete(self, messages, **kw):
        r = self.inner.complete(messages, **kw)
        if not messages[0].content.startswith(CRITIC) and r.content.startswith("{"):
            doc = json.loads(r.content)
            for c in doc.get("columns", []):
                c["business_name"], c["synonyms"] = "Field", []
            r.content = json.dumps(doc)
        return r


async def _job(factory, job_id) -> Job:
    async with factory() as s:
        return await s.get(Job, job_id)


async def _trial(factory, worker, params) -> Job:
    async with factory() as s:
        job = await enqueue(s, "model_trial", "demo_pg", requested_by="dyilmaz",
                            params=params)
        await s.commit()
    await worker.tick()
    return await _job(factory, job.id)


@pytest.mark.asyncio
async def test_a_model_registered_probed_trialled_switched_and_rolled_back(
        pg_session_factory, fake_llm, fake_embedder, tmp_path, monkeypatch):
    from test_golden_pg import GoldenModel, _plan
    from test_jobs_pg import _subset
    from test_review_pg import AYSE, _col, _world

    from gaia.datasources.crypto import new_key
    from gaia.evaluation.quality import select_runs
    from gaia.evaluation.store import load_runs
    from gaia.query.golden import load_golden

    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    monkeypatch.setenv("QWEN_KEY", "sk-qwen")
    factory = pg_session_factory
    base, review = await _world(factory, fake_llm, fake_embedder)
    settings = base.model_copy(update={"jobs": base.jobs.model_copy(update={
        "heartbeat_seconds": 0.05, "runs_dir": str(tmp_path / "runs")})})
    configured = settings.llm.model

    # -- registered beside the configured model ---------------------------------------------
    async with factory() as s:
        qwen = await add_model(s, settings, name="qwen", base_url="https://gw.example/v1",
                               model_id="qwen-max", key_env="QWEN_KEY", by="dyilmaz",
                               options={"concurrency": 2})
        typed = await add_model(s, settings, name="other", base_url="https://other/v1",
                                model_id="other-1", key="sk-typed", by="dyilmaz")
        for bad, match in (({"name": "qwen"}, "registered already"),
                           ({"name": configured}, "the configured model"),
                           ({"name": "two words"}, "without spaces"),
                           ({"name": "x", "key_env": None}, "needs a key"),
                           ({"name": "x", "key": "k"}, "not both"),
                           ({"name": "x", "options": {"api_key": "k"}}, "unknown option")):
            args = {"name": "x", "base_url": "https://gw/v1", "model_id": "m",
                    "key_env": "QWEN_KEY", "by": "dyilmaz", **bad}
            with pytest.raises(ModelError, match=match):
                await add_model(s, settings, **args)
        await s.commit()
    assert qwen.status == "unverified" and qwen.has_key and not qwen.usable
    async with factory() as s:
        row = await s.scalar(sa.select(Model).where(Model.name == "other"))
        assert row.secret and b"sk-typed" not in bytes(row.secret)  # encrypted, bound to the row
        from gaia.models.registry import llm_config_of

        assert llm_config_of(await get_model(s, settings, "other"), settings).api_key == \
            "sk-typed"
        assert [m.name for m in await list_models(s, settings)] == [configured, "qwen", "other"]
    assert typed.has_key

    # -- probed: refused to serve until it passes -------------------------------------------
    async with factory() as s:
        with pytest.raises(ModelError, match="unverified: probe it first"):
            await switch(s, settings, "answering", "qwen", by="dyilmaz", force=True)
        failed = await probe_model(s, settings, "qwen", client_factory=lambda cfg: ProbeNoJson())
        assert failed.status == "failed"
        assert {c["name"]: c["ok"] for c in failed.probe} == {"plain": True, "json": False,
                                                              "thinking": False}
        ok = await probe_model(s, settings, "qwen", client_factory=lambda cfg: ProbeOK())
        assert ok.status == "verified" and ok.probed_at is not None
        with pytest.raises(ModelError, match="make check-llm"):
            await probe_model(s, settings, configured)
        await s.commit()
    monkeypatch.delenv("QWEN_KEY")
    async with factory() as s:  # the key's variable unset: the probe says so, calls nothing
        unset = await probe_model(s, settings, "qwen", client_factory=lambda cfg: 1 / 0)
        assert unset.status == "failed" and "QWEN_KEY is not set" in unset.probe[0]["detail"]
        await s.rollback()
    monkeypatch.setenv("QWEN_KEY", "sk-qwen")

    # -- a switch needs a trial, or force (recorded); a rollback returns -------------------
    async with factory() as s:
        with pytest.raises(ModelError, match="no trial of qwen for answering"):
            await switch(s, settings, "answering", "qwen", by="dyilmaz")
        forced = await switch(s, settings, "answering", "qwen", by="dyilmaz", force=True,
                              note="the provider retires the old one")
        await s.commit()
    assert forced.forced and forced.previous == configured and forced.rollback_until
    async with factory() as s:
        assert (await effective_settings(s, settings, "answering")).llm.model == "qwen-max"
        assert (await effective_settings(s, settings, "reasoning")).llm is settings.llm
        with pytest.raises(ModelError, match="serves answering already"):
            await switch(s, settings, "answering", "qwen", by="dyilmaz", force=True)
        with pytest.raises(ModelError, match="serves answering: switch first"):
            await retire(s, settings, "qwen", by="dyilmaz")
        with pytest.raises(ModelError, match="rollback period of answering ended"):
            await rollback(s, settings, "answering", by="dyilmaz",
                           now=datetime.now(UTC) + timedelta(days=31))
        back = await rollback(s, settings, "answering", by="dyilmaz")
        await s.commit()
    assert (back.model_name, back.previous) == (configured, "qwen")
    async with factory() as s:
        assert (await effective_settings(s, settings, "answering")).llm is settings.llm
        with pytest.raises(ModelError, match="never was switched|nothing to roll back"):
            await rollback(s, settings, "reasoning", by="dyilmaz")

    # -- the answering trial: interleaved golden runs, the broken question named ------------
    golden_file = _subset(tmp_path, ["g01", "g02", "g05", "g10"])
    golden = load_golden(golden_file)

    class Reporting(GoldenModel):
        def __init__(self, golden, name):
            super().__init__(golden)
            self.model = name

        def complete(self, messages, **kw):
            r = super().complete(messages, **kw)
            r.model = self.model
            return r

    good, worse = Reporting(golden, configured), Reporting(golden, "qwen-max")
    g01 = next(q for q in golden.questions if q.id == "g01")
    worse.first[g01.question] = _plan(sql=g01.sql.replace("AND status <> 'cancelled'", ""))
    asked: list[str] = []

    def answering_models(cfg):
        asked.append(cfg.model)
        return worse if cfg.model == "qwen-max" else good

    worker = Worker(settings, factory, deps=JobDeps(client_factory=answering_models,
                                                    embedder_factory=lambda: fake_embedder))
    j = await _trial(factory, worker, {"model": "qwen", "purpose": "answering",
                                       "file": str(golden_file), "runs": 3,
                                       "no_vectors": True})
    assert j.state == "done", (j.state, j.error, j.result)
    a = j.result["answering"]
    assert a["against"] == configured and a["breaks"] == ["g01"], a
    assert a["verdict"] == "fails", a
    assert a["candidate_runs"]["correct"] < a["current_runs"]["correct"]
    assert asked[:4] == [configured, "qwen-max", configured, "qwen-max"]  # interleaved
    assert len(asked) == 6
    assert "reasoning" not in j.result and j.result["preview"]["answering"]
    text = "\n".join(trial_lines(j.result))
    assert "answering against" in text and "broken: g01" in text
    # the candidate's runs are left out of the ordinary report; the current model's count
    async with factory() as s:
        views = await load_runs(s, "demo_pg")
    sel = select_runs(views, last=10)
    assert len(sel.trials) == 3 and all("model trial: qwen" in r.build for r in sel.trials)
    assert len(sel.shown) == 3 and not any("model trial" in (r.build or "") for r in sel.shown)

    # -- the reasoning trial: a shadow, judged against the SME's decisions -------------------
    accepted = ["amount", "invoice_id", "issued_on"]
    for col in accepted:
        c = await _col(factory, "finance.invoices", col)
        await review.decide_column("demo_pg", c.id, "accept", AYSE)
    async with factory() as s:
        before = await s.scalar(sa.select(sa.func.count()).select_from(sa.text(
            "semantics.column_semantic")))
    candidate = Renaming(fake_llm)
    worker = Worker(settings, factory, deps=JobDeps(
        client_factory=lambda cfg: candidate if cfg.model == "qwen-max" else fake_llm,
        embedder_factory=lambda: fake_embedder))
    j = await _trial(factory, worker, {"model": "qwen", "purpose": "reasoning"})
    assert j.state == "done", (j.state, j.error, j.result)
    r = j.result["reasoning"]
    assert r["tables"] == ["finance.invoices"]
    assert (r["current"]["right"], r["current"]["n"]) == (3, 3)
    assert (r["candidate"]["partly"], r["candidate"]["right"]) == (3, 0)
    assert r["candidate"]["misses"][0]["answer"].startswith("Field")
    assert r["candidate"]["tokens_per_table"] > 0
    p = j.result["preview"]
    assert p["tables"] >= 20 and p["sme_decisions_kept"] == 3 and p["calibrated"] == []
    assert p["reasoning"]["tokens"] > 0 and p["reasoning"]["nights"] == 1
    async with factory() as s:  # the shadow wrote nothing
        after = await s.scalar(sa.select(sa.func.count()).select_from(sa.text(
            "semantics.column_semantic")))
    assert after == before

    # a trial of a model that serves already, or of one not usable: refused
    j = await _trial(factory, worker, {"model": configured, "purpose": "reasoning"})
    assert j.state == "refused" and "reasons already" in j.error

    # -- switched with its trial; the nightly refresh re-reasons with it --------------------
    async with factory() as s:
        sw = await switch(s, settings, "reasoning", "qwen", by="dyilmaz")
        await s.commit()
    assert not sw.forced and sw.trial_job_id is not None
    calls = fake_llm.calls.total()
    worker = Worker(settings, factory, deps=JobDeps(
        client_factory=lambda cfg: candidate if cfg.model == "qwen-max" else 1 / 0,
        embedder_factory=lambda: fake_embedder))
    async with factory() as s:
        job = await enqueue(s, "refresh", "demo_pg", requested_by="schedule",
                            params={"no_claim_tests": True})
        await s.commit()
    await worker.tick()
    j = await _job(factory, job.id)
    assert j.state == "done", (j.state, j.error, j.result)
    assert j.result["reason"]["reasoned"] >= 20 and fake_llm.calls.total() > calls
    async with factory() as s:
        counts = (await meanings_by_model(s))["demo_pg"]
        kept = await s.scalar(sa.select(sa.func.count()).select_from(sa.text(
            "semantics.column_semantic cs")).where(sa.text(
                "cs.is_current AND cs.reviewed_by = 'ayse'")))
    assert set(counts) == {"qwen-max", "SME-decided"} and counts["SME-decided"] == 3
    assert kept == 3  # the SME's decisions stay (§24d)

    # -- retirement: not while it serves, nor while it is a rollback target ------------------
    async with factory() as s:
        with pytest.raises(ModelError, match="serves reasoning: switch first"):
            await retire(s, settings, "qwen", by="dyilmaz")
        # back to the configured model by a switch (no trial needed): qwen stays a rollback
        # target for the rollback period
        sw = await switch(s, settings, "reasoning", configured, by="dyilmaz")
        assert sw.previous == "qwen" and sw.rollback_until and not sw.forced
        with pytest.raises(ModelError, match="may still roll back to qwen"):
            await retire(s, settings, "qwen", by="dyilmaz")
        await s.execute(sa.update(ModelAssignment).values(
            rollback_until=datetime.now(UTC) - timedelta(days=1)))
        gone = await retire(s, settings, "other", by="dyilmaz")
        done = await retire(s, settings, "qwen", by="dyilmaz")
        await s.commit()
    assert gone.status == done.status == "retired" and not gone.row.secret
    async with factory() as s:
        assert [m.name for m in await list_models(s, settings)] == [configured]
        with pytest.raises(ModelError, match="retired"):
            await probe_model(s, settings, "qwen", client_factory=lambda cfg: ProbeOK())
    j = await _trial(factory, worker, {"model": "qwen", "purpose": "answering"})
    assert j.state == "refused" and "retired" in j.error


@pytest.mark.asyncio
async def test_the_command_line(pg_session_factory, monkeypatch, capsys):
    from gaia.datasources.crypto import new_key
    from gaia.db.engine import dispose_engine
    from gaia.tools import model as tool

    factory = pg_session_factory
    monkeypatch.setenv("GAIA_METADATA_URL", os.environ["GAIA_TEST_DATABASE_URL"])
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    monkeypatch.setenv("QWEN_KEY", "sk-qwen")
    await dispose_engine()

    def ns(**kw):
        base = {"target": "demo_pg", "command": None, "all": False, "name": None,
                "base_url": None, "model_id": None, "key_env": None, "key": False,
                "locality": "remote", "option": None, "purpose": None, "param": None,
                "id": None, "force": False, "note": None}
        return argparse.Namespace(**{**base, **kw})

    assert await tool._run(ns(command="add", name="qwen", base_url="https://gw/v1",
                              model_id="qwen-max", key_env="QWEN_KEY",
                              option=["concurrency=2"])) == 0
    assert "registered qwen (unverified)" in capsys.readouterr().out
    assert await tool._run(ns(command="trial", name="qwen", purpose="both")) == 1
    assert "unverified: probe it first" in capsys.readouterr().err
    assert await tool._run(ns(command="switch", name="qwen", purpose="answering",
                              force=True)) == 1
    async with factory() as s:
        await s.execute(sa.update(Model).values(status="verified"))
        await s.commit()
    assert await tool._run(ns(command="switch", name="qwen", purpose="answering")) == 1
    assert "no trial of qwen" in capsys.readouterr().err
    assert await tool._run(ns(command="trial", name="qwen", purpose="both")) == 0
    assert "queued a trial of qwen on demo_pg" in capsys.readouterr().out
    assert await tool._run(ns(command="switch", name="qwen", purpose="answering", force=True,
                              note="provider change")) == 0
    assert "rollback possible until" in capsys.readouterr().out
    assert await tool._run(ns(command="list")) == 0
    out = capsys.readouterr().out
    assert "serves answering" in out and "replacing" in out and "last trial" in out
    assert await tool._run(ns(command="rollback", purpose="answering")) == 0
    assert await tool._run(ns(command="show")) == 0  # queued: no result yet
    async with factory() as s:
        actions = (await s.scalars(sa.text(
            "SELECT action FROM audit.auth_event ORDER BY seq"))).all()
        alerts = (await s.scalars(sa.select(Alert.message))).all()
    assert actions == ["model_added", "job_started", "model_switched", "model_rolled_back"]
    assert any("without a trial (forced)" in m for m in alerts) and len(alerts) == 2
