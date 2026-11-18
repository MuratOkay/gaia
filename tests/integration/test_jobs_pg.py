"""Runs that run themselves (Phase 7C, D98) on ``demo_pg`` with the scripted model.

The queue (one active job per kind and data source, claimed once, cancelled, a silent worker
reaped), schedules (queued once however late, never doubled), and the jobs themselves: a
**refresh** that stops at its token budget and finishes the next night, costs nothing when
nothing changed, picks up exactly a table the target gained, is refused outside its window
and stops when cancelled; a **drift watch** that sets its baseline, then names a question a
worse model breaks and sees the provider's model change; the week's **sample**."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
import sqlalchemy as sa
import yaml

from gaia.db.models.ops import Alert, Job, Schedule
from gaia.jobs.kinds import JobDeps
from gaia.jobs.queue import (
    JobRefused,
    claim,
    enqueue,
    heartbeat,
    reap_stale,
    request_cancel,
)
from gaia.jobs.schedules import add_schedule, enqueue_due
from gaia.jobs.worker import Worker

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
ROOT = Path(__file__).resolve().parents[2]
PROBES = ("legacy.zz_gaia_probe", "legacy.zz_gaia_probe2")


def _admin_url() -> str | None:
    """The target's owner — to make a deliberate schema change (the seed's own setting)."""
    url = os.environ.get("GAIA_PG_TARGET_ADMIN_URL")
    if not url:
        from dotenv import dotenv_values

        url = dotenv_values(ROOT / ".env").get("GAIA_PG_TARGET_ADMIN_URL")
    if url and urlsplit(url).path != urlsplit(URL or "").path:
        return None  # another database than the tests read
    return url


def _target_sql(*statements: str) -> None:
    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        for st in statements:
            conn.execute(st)


def _drop_probes() -> None:
    _target_sql(*(f"DROP TABLE IF EXISTS {t}" for t in PROBES))


def _settings(**jobs):
    from test_profiling_pg import _settings as base

    s = base()
    return s.model_copy(update={"jobs": s.jobs.model_copy(update={
        "heartbeat_seconds": 0.05, "poll_seconds": 0.05, **jobs})})


async def _job(factory, job_id) -> Job:
    async with factory() as s:
        return await s.get(Job, job_id)


async def _start(factory, kind, params=None, who="admin") -> Job:
    async with factory() as s:
        job = await enqueue(s, kind, "demo_pg", requested_by=who, params=params or {})
        await s.commit()
        return job


@pytest.mark.asyncio
async def test_the_queue_and_the_schedules(pg_session_factory):
    factory = pg_session_factory
    async with factory() as s:
        first = await enqueue(s, "refresh", "demo_pg", requested_by="dyilmaz", known={"demo_pg"})
        await s.commit()
        with pytest.raises(JobRefused, match="already queued"):
            await enqueue(s, "refresh", "demo_pg", requested_by="akaya")
        with pytest.raises(JobRefused, match="no data source"):
            await enqueue(s, "refresh", "nowhere", requested_by="x", known={"demo_pg"})
        with pytest.raises(JobRefused, match="kind is one of"):
            await enqueue(s, "dance", "demo_pg", requested_by="x")
        other = await enqueue(s, "sample", "demo_pg", requested_by="x")
        await s.commit()

    # two workers at once: each job is claimed by exactly one
    async def take(name):
        async with factory() as s:
            return await claim(s, name)
    got = await asyncio.gather(take("w1"), take("w2"), take("w3"))
    taken = sorted(str(j.id) for j in got if j is not None)
    assert taken == sorted([str(first.id), str(other.id)])
    assert sum(j is None for j in got) == 1

    # a cancellation of a running job is read by its heartbeat
    async with factory() as s:
        assert not await heartbeat(s, first.id, progress={"step": "x"}, tokens=5, calls=1)
        await request_cancel(s, first.id, by="dyilmaz")
        await s.commit()
        assert await heartbeat(s, first.id, progress={}, tokens=5, calls=1)
    # a silent worker: its job fails, with an alert
    async with factory() as s:
        await s.execute(sa.update(Job).where(Job.id == other.id).values(
            heartbeat_at=datetime.now(UTC) - timedelta(minutes=30)))
        await s.commit()
        reaped = await reap_stale(s, stale_after=timedelta(minutes=10))
    assert [j.id for j in reaped] == [other.id]
    lost = await _job(factory, other.id)
    assert lost.state == "failed" and "stopped answering" in lost.error
    async with factory() as s:
        kinds = sorted((await s.scalars(sa.select(Alert.kind))).all())
    assert kinds == ["job_failed", "worker_lost"]
    # a queued job cancelled at once; a finished one cannot be
    async with factory() as s:
        q = await enqueue(s, "drift_watch", "demo_pg", requested_by="x")
        await request_cancel(s, q.id, by="x")
        await s.commit()
        assert q.state == "cancelled"
        with pytest.raises(JobRefused, match="cancelled already"):
            await request_cancel(s, q.id, by="x")

    # schedules: queued once however late, next time from now, never doubled
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    async with factory() as s:
        sched = await add_schedule(s, "demo_pg", "sample", "weekly mon 06:00", created_by="x",
                                   now=now)
        await s.commit()
        assert sched.next_due_at > now and sched.next_due_at.astimezone().weekday() == 0
        late = sched.next_due_at + timedelta(days=15)  # the worker was down for two weeks
        queued = await enqueue_due(s, now=late)
    assert len(queued) == 1 and isinstance(queued[0][1], Job)
    async with factory() as s:
        again = await enqueue_due(s, now=late + timedelta(minutes=1))
        row = await s.scalar(sa.select(Schedule))
        assert again == [] and row.next_due_at > late
        jobs = (await s.scalars(sa.select(Job).where(Job.kind == "sample"))).all()
    assert len([j for j in jobs if j.schedule_id == row.id]) == 1
    async with factory() as s:  # due again while its job still waits: noted, not doubled
        due = await enqueue_due(s, now=row.next_due_at + timedelta(minutes=1))
    assert "already queued" in due[0][1]


@pytest.mark.asyncio
async def test_a_refresh_runs_what_changed_within_its_budget_window_and_cancellation(
        pg_session_factory, fake_llm, fake_embedder):
    if not _admin_url():
        pytest.skip("GAIA_PG_TARGET_ADMIN_URL not set (the target's owner, as for "
                    "`make pg-target-seed`): needed to change the target's schema")
    factory = pg_session_factory
    settings = _settings()
    deps = JobDeps(llm_factory=lambda: fake_llm, embedder_factory=lambda: fake_embedder)
    worker = Worker(settings, factory, deps=deps)
    _drop_probes()
    try:
        # the first night: everything is new — the budget stops reasoning part of the way
        job = await _start(factory, "refresh", {"budget": 5000})
        assert (await worker.tick()).id == job.id
        j = await _job(factory, job.id)
        assert j.state == "partial", (j.state, j.error)
        r = j.result
        assert r["introspect"]["first"] is True and r["profile"]["profiled"] >= 20
        assert r["reason"]["stopped"] > 0 and r["reason"]["reasoned"] >= 1
        assert r["stopped"] == "the token budget of 5,000 is spent"
        assert j.tokens >= 5000 and j.calls >= 2
        assert "claim_tests" not in r  # nothing after the budget is spent
        # the next night takes up the rest
        job = await _start(factory, "refresh", {"budget": 0})
        await worker.tick()
        j = await _job(factory, job.id)
        assert j.state == "done", (j.state, j.error, j.result)
        assert j.result["reason"].get("stopped", 0) == 0
        assert j.result["reason"]["reasoned"] == r["reason"]["stopped"]
        # nothing changed: no data query for profiles, no model call
        calls = fake_llm.calls.total()
        job = await _start(factory, "refresh")
        await worker.tick()
        j = await _job(factory, job.id)
        assert j.state == "done" and j.tokens == 0 and fake_llm.calls.total() == calls
        assert j.result["introspect"]["added"] == 0
        assert "profiled" not in j.result["profile"] and j.result["profile"]["reused"] >= 20
        assert "reasoned" not in j.result["reason"]

        # the target gains a table: exactly it is profiled and reasoned
        role = urlsplit(URL).username
        _target_sql("CREATE TABLE legacy.zz_gaia_probe (id integer PRIMARY KEY, note text)",
                    "INSERT INTO legacy.zz_gaia_probe SELECT g, 'n' || g "
                    "FROM generate_series(1, 50) g",
                    f'GRANT SELECT ON legacy.zz_gaia_probe TO "{role}"',
                    "ANALYZE legacy.zz_gaia_probe")
        job = await _start(factory, "refresh")
        await worker.tick()
        j = await _job(factory, job.id)
        assert j.state == "done", (j.error, j.result)
        assert j.result["introspect"]["added"] == 1
        assert any("zz_gaia_probe" in c for c in j.result["introspect"]["changes"])
        assert j.result["profile"].get("profiled") == 1
        assert j.result["reason"].get("reasoned") == 1
        assert fake_llm.tables[("semantics", "legacy.zz_gaia_probe")] == 1

        # outside its window: refused before anything runs
        narrow = settings.model_copy(update={"profiling": settings.profiling.model_copy(
            update={"window": "01:00-02:00"})})
        noon = datetime.now().replace(hour=12, minute=0)
        w2 = Worker(narrow, factory, deps=JobDeps(llm_factory=lambda: fake_llm,
                                                  embedder_factory=lambda: fake_embedder,
                                                  clock=lambda: noon.astimezone(UTC)))
        job = await _start(factory, "refresh")
        await w2.tick()
        j = await _job(factory, job.id)
        assert j.state == "refused" and "outside the run window 01:00-02:00" in j.error

        # cancelled while reasoning: the table under way finishes, the next is not started
        _target_sql("ALTER TABLE legacy.zz_gaia_probe ADD COLUMN extra integer",
                    "CREATE TABLE legacy.zz_gaia_probe2 (id integer PRIMARY KEY)",
                    f'GRANT SELECT ON legacy.zz_gaia_probe2 TO "{role}"')
        one = settings.model_copy(update={"llm": settings.llm.model_copy(
            update={"concurrency": 1})})

        class Cancelling:
            def __init__(self) -> None:
                self.model = "fake"

            def complete(self, messages, **kw):
                w3.stop_current()  # as the heartbeat does when someone asks
                return fake_llm.complete(messages, **kw)

        w3 = Worker(one, factory, deps=JobDeps(llm_factory=Cancelling,
                                               embedder_factory=lambda: fake_embedder))
        job = await _start(factory, "refresh")
        await w3.tick()
        j = await _job(factory, job.id)
        assert j.state == "cancelled" and j.result["stopped"] == "cancelled"
        assert j.result["reason"] == {"reasoned": 1, "stopped": 1, "embeddings": 0} or (
            j.result["reason"]["reasoned"] == 1 and j.result["reason"]["stopped"] == 1)
        # the worker notices, the next run does the rest
        job = await _start(factory, "refresh")
        await worker.tick()
        j = await _job(factory, job.id)
        assert j.state == "done" and j.result["reason"]["reasoned"] == 1
    finally:
        _drop_probes()
    async with factory() as s:
        events = (await s.scalars(sa.text(
            "SELECT message FROM ops.job_event WHERE message LIKE 'introspect:%'"))).all()
    assert any("zz_gaia_probe" in m for m in events)


def _subset(tmp_path: Path, ids: list[str]) -> Path:
    doc = yaml.safe_load((ROOT / "infra" / "pg-target" / "golden.yaml").read_text())
    doc["questions"] = [q for q in doc["questions"] if q["id"] in ids]
    path = tmp_path / "golden_subset.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_the_drift_watch_names_a_break_and_a_new_model(pg_session_factory, fake_llm,
                                                            fake_embedder, tmp_path):
    from test_golden_pg import GoldenModel, _plan
    from test_review_pg import _world

    from gaia.query.golden import load_golden

    factory = pg_session_factory
    base_settings, _r = await _world(factory, fake_llm, fake_embedder)
    settings = base_settings.model_copy(update={"jobs": base_settings.jobs.model_copy(update={
        "heartbeat_seconds": 0.05, "runs_dir": str(tmp_path / "runs")})})
    golden_file = _subset(tmp_path, ["g01", "g02", "g05", "g10"])
    golden = load_golden(golden_file)
    params = {"file": str(golden_file), "runs": 3, "no_vectors": True}

    class Reporting(GoldenModel):
        def __init__(self, golden, name):
            super().__init__(golden)
            self.name = name

        def complete(self, messages, **kw):
            r = super().complete(messages, **kw)
            r.model = self.name
            return r

    good = Reporting(golden, "deepseek-v4.1-flash")
    worker = Worker(settings, factory, deps=JobDeps(llm_factory=lambda: good))
    job = await _start(factory, "drift_watch", params)
    await worker.tick()
    j = await _job(factory, job.id)
    assert j.state == "done", (j.error, j.result)
    assert len(j.result["runs"]) == 3 and j.result["verdict"] == "no baseline"
    assert j.result["answered_by"]["deepseek-v4.1-flash"] > 0

    # a week later the provider's name answers with another model, which breaks g01
    worse = Reporting(golden, "deepseek-v4.2-flash")
    g01 = next(q for q in golden.questions if q.id == "g01")
    worse.first[g01.question] = _plan(sql=g01.sql.replace("AND status <> 'cancelled'", ""))
    worker = Worker(settings, factory, deps=JobDeps(llm_factory=lambda: worse))
    job = await _start(factory, "drift_watch", params)
    await worker.tick()
    j = await _job(factory, job.id)
    r = j.result
    assert j.state == "done" and r["verdict"] == "fails" and r["breaks"] == ["g01"]
    assert r["baseline"] and set(r["baseline"]).isdisjoint(r["runs"])
    assert r["model_changed"] == {"new": ["deepseek-v4.2-flash"],
                                  "before": ["deepseek-v4.1-flash"]}
    async with factory() as s:
        alerts = {a.kind: a for a in (await s.scalars(sa.select(Alert))).all()}
    assert "broken g01" in alerts["drift"].message
    assert "deepseek-v4.2-flash" in alerts["model_changed"].message

    # no golden set for a data source: refused
    job = await _start(factory, "drift_watch", {"file": str(tmp_path / "none.yaml")})
    await worker.tick()
    assert (await _job(factory, job.id)).state == "refused"


@pytest.mark.asyncio
async def test_the_sample_job_and_the_worker_loop(pg_session_factory, fake_llm, fake_embedder):
    from test_review_pg import _world

    from gaia.db.models.semantics import ReviewSample

    factory = pg_session_factory
    settings, _r = await _world(factory, fake_llm, fake_embedder)
    settings = settings.model_copy(update={"jobs": settings.jobs.model_copy(update={
        "heartbeat_seconds": 0.05, "poll_seconds": 0.05})})
    async with factory() as s:
        await add_schedule(s, "demo_pg", "sample", "daily 00:00", created_by="x",
                           now=datetime.now(UTC) - timedelta(days=2))
        await s.commit()
    worker = Worker(settings, factory)
    stop = asyncio.Event()
    ran = await worker.loop(stop, once=True)
    assert ran == 1
    async with factory() as s:
        job = await s.scalar(sa.select(Job).where(Job.kind == "sample"))
        drawn = await s.scalar(sa.select(sa.func.count()).select_from(ReviewSample))
    assert job.state == "done" and job.requested_by == "schedule"
    assert job.result["new"] is True and job.result["drawn"] == drawn > 0
    assert await worker.loop(stop, once=True) == 0  # nothing due, nothing queued


@pytest.mark.asyncio
async def test_the_command_line(pg_session_factory, monkeypatch, capsys):
    from gaia.db.engine import dispose_engine
    from gaia.tools import jobs as tool

    factory = pg_session_factory
    monkeypatch.setenv("GAIA_METADATA_URL", os.environ["GAIA_TEST_DATABASE_URL"])
    await dispose_engine()

    import argparse

    def ns(**kw):
        base = {"target": "demo_pg", "last": 20, "command": None, "id": None, "kind": None,
                "param": None, "words": []}
        return argparse.Namespace(**{**base, **kw})

    assert await tool._run(ns(command="start", kind="sample")) == 0
    assert "queued sample of demo_pg" in capsys.readouterr().out
    assert await tool._run(ns(command="start", kind="sample")) == 1
    assert "already queued" in capsys.readouterr().err
    assert await tool._run(ns(command="schedule", words=["defaults"])) == 0
    out = capsys.readouterr().out
    assert "refresh: daily 02:00" in out and "drift_watch: weekly sun 04:00" in out
    assert await tool._run(ns(command="schedule", words=["off", "refresh"])) == 0
    assert await tool._run(ns(command="schedules")) == 0
    out = capsys.readouterr().out
    assert "refresh      daily 02:00" in out and "OFF" in out
    assert await tool._run(ns(command="list")) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "sample" in line and "queued" in line
    assert await tool._run(ns(command="cancel", id=line.split()[0])) == 0
    assert "cancelled" in capsys.readouterr().out
    async with factory() as s:
        actions = (await s.scalars(sa.text(
            "SELECT action FROM audit.auth_event ORDER BY seq"))).all()
    assert actions == ["job_started", "schedule_changed", "schedule_changed", "job_cancelled"]
