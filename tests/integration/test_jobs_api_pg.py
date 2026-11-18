"""Jobs, schedules and alerts over HTTP (Phase 7C, D98): administrators start, cancel and
schedule; auditors read; everyone else is refused — and every change is audited."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from test_auth_api_pg import PASSWORDS, _client, _login, api  # noqa: F401  (fixture)

from gaia.db.models.audit import AuthEvent
from gaia.jobs.queue import alert


@pytest.mark.asyncio
async def test_jobs_over_http(api):  # noqa: F811
    app, _auth, factory = api
    async with await _client(app) as deniz, await _client(app) as berk, \
            await _client(app) as ayse:
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        await _login(berk, "bdemir", PASSWORDS["bdemir"])
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        hd = {"X-CSRF-Token": deniz.cookies.get("gaia_csrf")}
        hb = {"X-CSRF-Token": berk.cookies.get("gaia_csrf")}

        r = await deniz.post("/api/jobs", json={"kind": "refresh", "data_source": "demo_pg",
                                                "params": {"budget": 100000}}, headers=hd)
        assert r.status_code == 201 and r.json()["state"] == "queued"
        job = r.json()["id"]
        assert (await deniz.post("/api/jobs", json={"kind": "refresh",
                                                    "data_source": "demo_pg"},
                                 headers=hd)).status_code == 400  # already queued
        r = await deniz.post("/api/jobs", json={"kind": "refresh", "data_source": "demo_pg",
                                                "params": {"api_key": "x"}}, headers=hd)
        assert r.status_code == 400 and "unknown parameter" in r.json()["detail"]
        assert (await deniz.post("/api/jobs", json={"kind": "refresh",
                                                    "data_source": "nowhere"},
                                 headers=hd)).status_code == 400
        assert (await deniz.post("/api/jobs", json={"kind": "sample",
                                                    "data_source": "demo_pg"})
                ).status_code == 401  # no CSRF header

        # the auditor reads, and may not start; an SME may do neither
        listed = (await berk.get("/api/jobs")).json()
        assert [j["id"] for j in listed["jobs"]] == [job] and "demo_pg" in listed["data_sources"]
        assert (await berk.get(f"/api/jobs/{job}")).json()["events"][0]["message"].startswith(
            "queued by dyilmaz")
        assert (await berk.post("/api/jobs", json={"kind": "sample", "data_source": "demo_pg"},
                                headers=hb)).status_code == 403
        assert (await ayse.get("/api/jobs")).status_code == 403

        r = await deniz.post(f"/api/jobs/{job}/cancel", headers=hd)
        assert r.json()["state"] == "cancelled"
        assert (await deniz.post(f"/api/jobs/{job}/cancel", headers=hd)).status_code == 400

        r = await deniz.put("/api/jobs/schedules", json={"data_source": "demo_pg",
                                                         "kind": "refresh",
                                                         "when": "daily 02:30"}, headers=hd)
        assert r.status_code == 200 and r.json()["when"] == "daily 02:30"
        sid = r.json()["id"]
        assert (await deniz.put("/api/jobs/schedules", json={
            "data_source": "demo_pg", "kind": "refresh", "when": "sometimes"},
            headers=hd)).status_code == 400
        r = await deniz.post(f"/api/jobs/schedules/{sid}/enabled", json={"enabled": False},
                             headers=hd)
        assert r.json()["enabled"] is False
        assert [x["when"] for x in (await berk.get("/api/jobs/schedules")).json()] == [
            "daily 02:30"]

        async with factory() as s:
            a = await alert(s, "drift", "the golden set of demo_pg got worse: broken g01",
                            data_source="demo_pg")
            await s.commit()
        assert [x["message"] for x in (await deniz.get("/api/jobs/alerts")).json()] == [
            "the golden set of demo_pg got worse: broken g01"]
        assert (await berk.get("/api/jobs/alerts")).status_code == 403
        r = await deniz.post(f"/api/jobs/alerts/{a.id}/ack", headers=hd)
        assert r.json()["acknowledged_by"] == "dyilmaz"
        assert (await deniz.get("/api/jobs/alerts")).json() == []

    async with factory() as s:
        actions = (await s.scalars(sa.select(AuthEvent.action).where(AuthEvent.action.in_(
            ("job_started", "job_cancelled", "schedule_changed", "alert_acknowledged")))
            .order_by(AuthEvent.seq))).all()
    assert actions == ["job_started", "job_cancelled", "schedule_changed", "schedule_changed",
                       "alert_acknowledged"]
