"""The worker (Phase 7C, P7-6; D98): ``make worker``.

One process. Each turn: mark running jobs whose worker went silent as failed (an alert each);
queue what the schedules say is due; claim the oldest queued job and run it — one at a time
(P7-6: concurrency per data source stays D67's locks, and a single worker keeps the target's
load and the gateway's slots predictable). While a job runs, a heartbeat every
``jobs.heartbeat_seconds`` says it is alive, publishes its progress and tokens, and reads
whether a cancellation was asked. Stopping the worker (Ctrl-C, SIGTERM) asks the running job
to stop at its next table, records it, and exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import Settings
from gaia.db.models.catalog import DataSource
from gaia.db.models.ops import Job
from gaia.jobs.kinds import KINDS, JobContext, JobDeps, Refused
from gaia.jobs.queue import claim, finish, heartbeat, reap_stale, worker_name
from gaia.jobs.queue import log as log_event
from gaia.jobs.schedules import enqueue_due
from gaia.jobs.signal import StopSignal

__all__ = ["Worker", "known_data_sources"]

logger = logging.getLogger(__name__)


async def known_data_sources(session, settings: Settings) -> set[str]:
    """The configured targets and every data source the catalog holds (4F's included)."""
    names = set(settings.targets)
    names |= {n for n in (await session.scalars(select(DataSource.name).where(
        DataSource.status != "tombstoned"))).all() if "~" not in n}
    return names


class Worker:
    def __init__(self, settings: Settings, factory: async_sessionmaker, *,
                 deps: JobDeps | None = None, name: str | None = None,
                 echo=None) -> None:
        self.settings = settings
        self.factory = factory
        self.deps = deps or JobDeps()
        self.name = name or worker_name()
        self.echo = echo or (lambda _m: None)
        self.current: StopSignal | None = None

    def _now(self) -> datetime:
        return self.deps.clock() if self.deps.clock else datetime.now(UTC)

    async def tick(self) -> Job | None:
        """One turn; returns the job it ran, or None when there was nothing to do."""
        cfg = self.settings.jobs
        now = self._now()
        async with self.factory() as s:
            for job in await reap_stale(s, stale_after=timedelta(
                    minutes=cfg.stale_after_minutes), now=now):
                self.echo(f"{job.kind} of {job.data_source} ({str(job.id)[:8]}): its worker "
                          "stopped answering — failed")
        async with self.factory() as s:
            known = await known_data_sources(s, self.settings)
            for schedule, queued in await enqueue_due(s, now=now, known=known):
                self.echo(f"schedule {schedule.kind} of {schedule.data_source}: "
                          + (f"queued {str(queued.id)[:8]}" if isinstance(queued, Job)
                             else f"not queued — {queued}"))
        async with self.factory() as s:
            job = await claim(s, self.name, now=now)
        if job is None:
            return None
        await self.run(job)
        return job

    async def run(self, job: Job) -> None:
        signal = StopSignal(clock=self.deps.clock)
        self.current = signal
        self.echo(f"{job.kind} of {job.data_source} ({str(job.id)[:8]}): started")

        async def log(message: str, level: str = "info") -> None:
            async with self.factory() as s:
                await log_event(s, job.id, message, level)
                await s.commit()
            self.echo(f"  {message}")

        ctx = JobContext(self.settings, self.factory, job, signal, self.deps, log)
        beat = asyncio.create_task(self._beat(job.id, signal, ctx.progress))
        state, result, error = "failed", None, None
        try:
            outcome = await KINDS[job.kind](ctx)
            state, result, error = outcome.state, outcome.result, outcome.error
            if signal.stopped == "cancelled":
                state = "cancelled"
        except Refused as exc:
            state, error = "refused", str(exc)
        except Exception as exc:  # recorded, not swallowed: the job says what failed
            logger.exception("job %s failed", job.id)
            state, error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            self.current = None
        async with self.factory() as s:
            row = await s.get(Job, job.id)
            assert row is not None
            await finish(s, row, state, result=result, error=error,
                         tokens=signal.used_tokens, calls=signal.calls, now=self._now())
            row.progress = dict(ctx.progress)
            await s.commit()
        self.echo(f"{job.kind} of {job.data_source} ({str(job.id)[:8]}): {state}"
                  + (f" — {error}" if error else ""))

    async def _beat(self, job_id, signal: StopSignal, progress: dict[str, Any]) -> None:
        while True:
            await asyncio.sleep(max(0.05, self.settings.jobs.heartbeat_seconds))
            try:
                async with self.factory() as s:
                    if await heartbeat(s, job_id, progress=dict(progress),
                                       tokens=signal.used_tokens, calls=signal.calls):
                        signal.cancelled = True
            except Exception:  # a missed beat is not the job's failure
                logger.warning("heartbeat failed for job %s", job_id, exc_info=True)

    async def loop(self, stop: asyncio.Event, *, once: bool = False) -> int:
        """Run until ``stop`` is set (or, ``once``, until nothing is left to do). Returns how
        many jobs ran."""
        ran = 0
        while not stop.is_set():
            job = await self.tick()
            if job is not None:
                ran += 1
                continue
            if once:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.jobs.poll_seconds)
        return ran

    def stop_current(self) -> None:
        """Ask the running job to stop at its next table (the worker is shutting down)."""
        if self.current is not None:
            self.current.cancelled = True
