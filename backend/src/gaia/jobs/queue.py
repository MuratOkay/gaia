"""The job queue in the metadata store (Phase 7C, P7-6; D98).

* **Queued** by a person (``make jobs ARGS="start …"``, *Runs*) or a schedule; at most one
  queued or running job of a kind per data source (a partial unique index — a second request
  is refused and names the first).
* **Claimed** by the worker with ``FOR UPDATE SKIP LOCKED``: two workers never take one job.
* **Alive** while it runs: the worker writes ``heartbeat_at`` every ``jobs.heartbeat_seconds``
  and reads whether a cancellation was asked. A running job silent for
  ``jobs.stale_after_minutes`` lost its worker: it is marked failed, with an alert.
* **Cancelled**: a queued job at once; a running one at its next stop point (between tables).
* **Ends** done, partial (stopped early: budget, window, or a cancellation mid-step), failed
  or refused (it could not start: busy, outside its window, no golden set).
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.ops import JOB_KINDS, Alert, Job, JobEvent

__all__ = ["JobNotFound", "JobRefused", "acknowledge", "alert", "claim", "enqueue", "finish",
           "heartbeat", "job_view", "log", "open_alerts", "reap_stale", "request_cancel",
           "worker_name"]

FINISHED = ("done", "partial", "cancelled", "failed", "refused")


class JobRefused(ValueError):
    """A request that cannot be done (400): unknown kind, already queued, already over."""


class JobNotFound(LookupError):
    """No such job or alert (404)."""


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def enqueue(session: AsyncSession, kind: str, data_source: str, *, requested_by: str,
                  params: dict[str, Any] | None = None,
                  schedule_id: uuid.UUID | None = None, known: set[str] | None = None) -> Job:
    """Queue a job (flushed). ``known``: the data sources that exist — a name outside it is
    refused (the caller passes the configured targets and the catalog's data sources)."""
    if kind not in JOB_KINDS:
        raise JobRefused(f"kind is one of {', '.join(JOB_KINDS)}")
    if known is not None and data_source not in known:
        raise JobRefused(f"no data source {data_source!r} ({', '.join(sorted(known))})")
    active = await session.scalar(select(Job).where(
        Job.kind == kind, Job.data_source == data_source, Job.state.in_(("queued", "running"))))
    if active is not None:
        raise JobRefused(f"a {kind} of {data_source} is already {active.state} "
                         f"(job {str(active.id)[:8]})")
    job = Job(kind=kind, data_source=data_source, params=dict(params or {}), state="queued",
              requested_by=requested_by, schedule_id=schedule_id, progress={}, tokens=0,
              calls=0)
    session.add(job)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:  # queued by someone else in the same instant
        raise JobRefused(f"a {kind} of {data_source} is already queued") from None
    await log(session, job.id, f"queued by {requested_by}"
              + (f" with {params}" if params else ""))
    return job


async def claim(session: AsyncSession, worker: str, now: datetime | None = None) -> Job | None:
    """The oldest queued job, now running for ``worker`` (committed); None if none."""
    now = now or datetime.now(UTC)
    job = await session.scalar(select(Job).where(Job.state == "queued")
                               .order_by(Job.created_at, Job.id)
                               .with_for_update(skip_locked=True).limit(1))
    if job is None:
        await session.rollback()
        return None
    job.state, job.worker = "running", worker
    job.started_at = job.heartbeat_at = now
    await log(session, job.id, f"started by worker {worker}")
    await session.commit()
    return job


async def log(session: AsyncSession, job_id: uuid.UUID, message: str,
              level: str = "info") -> None:
    seq = (await session.scalar(select(func.max(JobEvent.seq)).where(
        JobEvent.job_id == job_id)) or 0) + 1
    session.add(JobEvent(job_id=job_id, seq=seq, level=level, message=message[:4000]))
    await session.flush()


async def finish(session: AsyncSession, job: Job, state: str, *, result: dict | None = None,
                 error: str | None = None, tokens: int = 0, calls: int = 0,
                 now: datetime | None = None) -> Job:
    job.state = state
    job.result = result
    job.error = (error or "")[:4000] or None
    job.tokens, job.calls = int(tokens), int(calls)
    job.finished_at = now or datetime.now(UTC)
    await log(session, job.id, f"{state}" + (f": {error}" if error else ""),
              "error" if state == "failed" else "info")
    if state in ("failed", "partial"):
        what = error or (result or {}).get("stopped") or state
        await alert(session, "job_failed" if state == "failed" else "job_partial",
                    f"{job.kind} of {job.data_source} {state}: {what}",
                    data_source=job.data_source, job_id=job.id)
    return job


async def request_cancel(session: AsyncSession, job_id: uuid.UUID, *, by: str) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise JobNotFound("no such job")
    if job.state in FINISHED:
        raise JobRefused(f"the job is {job.state} already")
    job.cancel_requested_at, job.cancel_requested_by = datetime.now(UTC), by
    if job.state == "queued":
        job.state, job.finished_at = "cancelled", job.cancel_requested_at
        await log(session, job.id, f"cancelled by {by} before it started")
    else:
        await log(session, job.id, f"cancellation asked by {by}: it stops at the next table")
    return job


async def reap_stale(session: AsyncSession, *, stale_after: timedelta,
                     now: datetime | None = None) -> list[Job]:
    """Running jobs whose worker stopped answering: failed, with an alert (committed)."""
    now = now or datetime.now(UTC)
    rows = (await session.scalars(select(Job).where(
        Job.state == "running", Job.heartbeat_at < now - stale_after)
        .with_for_update(skip_locked=True))).all()
    for job in rows:
        await finish(session, job, "failed", error=f"the worker {job.worker} stopped answering "
                     f"(no heartbeat since {job.heartbeat_at:%Y-%m-%d %H:%M} UTC)", now=now,
                     tokens=job.tokens, calls=job.calls)
        await alert(session, "worker_lost", f"{job.kind} of {job.data_source}: the worker "
                    "stopped answering — what it finished is kept; queue it again",
                    data_source=job.data_source, job_id=job.id)
    await session.commit()
    return list(rows)


async def alert(session: AsyncSession, kind: str, message: str, *,
                data_source: str | None = None, job_id: uuid.UUID | None = None,
                detail: dict[str, Any] | None = None) -> Alert:
    row = Alert(kind=kind, message=message[:2000], data_source=data_source, job_id=job_id,
                detail=detail)
    session.add(row)
    await session.flush()
    return row


async def open_alerts(session: AsyncSession, *, limit: int = 50) -> list[Alert]:
    return list((await session.scalars(select(Alert).where(Alert.acknowledged_at.is_(None))
                                       .order_by(Alert.created_at.desc()).limit(limit))).all())


async def acknowledge(session: AsyncSession, alert_id: uuid.UUID, *, by: str) -> Alert:
    row = await session.get(Alert, alert_id)
    if row is None:
        raise JobNotFound("no such alert")
    if row.acknowledged_at is None:
        row.acknowledged_at, row.acknowledged_by = datetime.now(UTC), by
    return row


async def heartbeat(session: AsyncSession, job_id: uuid.UUID, *, progress: dict[str, Any],
                    tokens: int, calls: int) -> bool:
    """Alive; returns whether a cancellation was asked (committed)."""
    await session.execute(update(Job).where(Job.id == job_id).values(
        heartbeat_at=datetime.now(UTC), progress=progress, tokens=tokens, calls=calls))
    asked = await session.scalar(select(Job.cancel_requested_at).where(Job.id == job_id))
    await session.commit()
    return asked is not None


def job_view(job: Job, events: list[JobEvent] | None = None) -> dict[str, Any]:
    out = {"id": str(job.id), "kind": job.kind, "data_source": job.data_source,
           "params": job.params, "state": job.state, "requested_by": job.requested_by,
           "schedule_id": str(job.schedule_id) if job.schedule_id else None,
           "worker": job.worker, "progress": job.progress, "result": job.result,
           "error": job.error, "tokens": job.tokens, "calls": job.calls,
           "cancel_requested_at": job.cancel_requested_at,
           "cancel_requested_by": job.cancel_requested_by, "created_at": job.created_at,
           "started_at": job.started_at, "heartbeat_at": job.heartbeat_at,
           "finished_at": job.finished_at}
    if events is not None:
        out["events"] = [{"seq": e.seq, "at": e.at, "level": e.level, "message": e.message}
                         for e in events]
    return out
