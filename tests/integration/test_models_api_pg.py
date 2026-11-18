"""Models over HTTP (Phase 7D, D101): the administrator registers, probes, trials, switches,
rolls back and retires; the auditor reads; an SME may do neither. A typed key is never shown
again; every change is audited; a switch raises an alert."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from test_auth_api_pg import PASSWORDS, _client, _login, api  # noqa: F401  (fixture)

from gaia.db.models.audit import AuthEvent
from gaia.db.models.ops import Alert
from gaia.llm.base import LLMResponse, Usage


class ProbeOK:
    def complete(self, messages, *, json_mode=False, thinking=False, **_kw):
        content = (json.dumps({"business_name": "Order total", "purpose": "p",
                               "confidence": 0.9}) if json_mode else "ready")
        return LLMResponse(content=content, model="qwen-max", finish_reason="stop",
                           usage=Usage(prompt_tokens=3, completion_tokens=3, total_tokens=6),
                           reasoning="thought" if thinking else None)


@pytest.mark.asyncio
async def test_models_over_http(api, monkeypatch):  # noqa: F811
    app, _auth, factory = api
    monkeypatch.setenv("QWEN_KEY", "sk-qwen")
    app.state.model_client_factory = lambda cfg: ProbeOK()
    async with await _client(app) as deniz, await _client(app) as berk, \
            await _client(app) as ayse:
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        await _login(berk, "bdemir", PASSWORDS["bdemir"])
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        hd = {"X-CSRF-Token": deniz.cookies.get("gaia_csrf")}
        hb = {"X-CSRF-Token": berk.cookies.get("gaia_csrf")}

        listed = (await berk.get("/api/models")).json()
        configured = listed["models"][0]
        assert configured["origin"] == "config" and configured["serves"] == ["reasoning",
                                                                          "answering"]
        assert listed["purposes"] == ["reasoning", "answering"] and listed["rollback_days"] == 30
        assert (await ayse.get("/api/models")).status_code == 403

        r = await deniz.post("/api/models", json={
            "name": "qwen", "base_url": "https://gw.example/v1", "model_id": "qwen-max",
            "key_env": "QWEN_KEY", "options": {"concurrency": 2}}, headers=hd)
        assert r.status_code == 201 and r.json()["status"] == "unverified"
        r = await deniz.post("/api/models", json={
            "name": "typed", "base_url": "https://other/v1", "model_id": "other-1",
            "key": "sk-secret-typed"}, headers=hd)
        assert r.status_code == 201 and r.json()["has_key"] is True
        assert "sk-secret-typed" not in (await deniz.get("/api/models")).text
        r = await deniz.post("/api/models", json={
            "name": "qwen", "base_url": "x", "model_id": "y", "key_env": "QWEN_KEY"},
            headers=hd)
        assert r.status_code == 400 and "registered already" in r.json()["detail"]
        assert (await berk.post("/api/models", json={
            "name": "b", "base_url": "x", "model_id": "y", "key_env": "QWEN_KEY"},
            headers=hb)).status_code == 403

        # a trial of an unprobed model, and a switch without a trial: refused
        r = await deniz.post("/api/models/qwen/trial", json={"data_source": "demo_pg"},
                             headers=hd)
        assert r.status_code == 400 and "probe it first" in r.json()["detail"]
        r = await deniz.post("/api/models/qwen/probe", headers=hd)
        assert r.status_code == 200 and r.json()["status"] == "verified"
        r = await deniz.post("/api/models/qwen/trial", json={
            "data_source": "demo_pg", "purpose": "answering", "params": {"api_key": "x"}},
            headers=hd)
        assert r.status_code == 400 and "unknown parameter" in r.json()["detail"]
        r = await deniz.post("/api/models/qwen/trial", json={
            "data_source": "demo_pg", "purpose": "answering", "params": {"runs": 3}},
            headers=hd)
        assert r.status_code == 201 and r.json()["kind"] == "model_trial"
        assert r.json()["params"] == {"runs": 3, "model": "qwen", "purpose": "answering"}
        r = await deniz.post("/api/models/switch", json={"purpose": "answering",
                                                         "model": "qwen"}, headers=hd)
        assert r.status_code == 400 and "no trial of qwen" in r.json()["detail"]
        r = await deniz.post("/api/models/switch", json={"purpose": "answering",
                                                         "model": "qwen", "force": True,
                                                         "note": "provider change"},
                             headers=hd)
        assert r.status_code == 200 and r.json()["forced"] is True
        assert r.json()["previous"] == configured["name"] and r.json()["rollback_until"]

        listed = (await berk.get("/api/models")).json()
        assert listed["serving"] == {"reasoning": configured["name"], "answering": "qwen"}
        assert listed["assignments"]["answering"]["note"] == "provider change"
        assert [t["params"]["model"] for t in listed["trials"]] == ["qwen"]
        assert (await deniz.delete("/api/models/qwen", headers=hd)).status_code == 400
        r = await deniz.post("/api/models/rollback", json={"purpose": "answering"}, headers=hd)
        assert r.status_code == 200 and r.json()["model"] == configured["name"]
        r = await deniz.post("/api/models/rollback", json={"purpose": "reasoning"}, headers=hd)
        assert r.status_code == 400 and "nothing to roll back" in r.json()["detail"]
        r = await deniz.delete("/api/models/typed", headers=hd)
        assert r.status_code == 200 and r.json()["status"] == "retired"

    async with factory() as s:
        actions = (await s.scalars(sa.select(AuthEvent.action).where(AuthEvent.action.in_(
            ("model_added", "model_probed", "job_started", "model_switched",
             "model_rolled_back", "model_retired"))).order_by(AuthEvent.seq))).all()
        alerts = (await s.scalars(sa.select(Alert.kind))).all()
    assert actions == ["model_added", "model_added", "model_probed", "job_started",
                       "model_switched", "model_rolled_back", "model_retired"]
    assert alerts == ["model_switched", "model_switched"]
