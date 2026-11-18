"""Schedules (Phase 7C, P7-7; D98): a kind of job queued by itself for a data source.

``daily`` at a local time of the host, or ``weekly`` on a weekday at a local time — as
``profiling.window`` is read in the host's local time. The worker queues what is due
(``enqueue_due``): each schedule row is locked while it is read (two workers never queue it
twice), queued once however late the worker comes (a worker down for three nights queues one
refresh, not three), and its next time is computed from *now*. A job of that kind already
queued or running for the data source is not doubled: the schedule notes it and moves on.

P7-7's defaults, as ``make jobs ARGS="schedule defaults TARGET"`` writes them: the refresh
nightly (inside the run window, if the data source has one), the drift watch weekly, the
calibration sample weekly on Monday morning.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.ops import Schedule
from gaia.jobs.queue import JobRefused, enqueue

__all__ = ["DEFAULTS", "WEEKDAYS", "add_schedule", "enqueue_due", "next_due", "parse_when",
           "schedule_view"]

SCHEDULED = ("refresh", "drift_watch", "sample")  # a model trial is started, never scheduled
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# P7-7: what `schedule defaults` sets up — (kind, every, weekday, at)
DEFAULTS = (("refresh", "daily", None, "02:00"), ("drift_watch", "weekly", 6, "04:00"),
            ("sample", "weekly", 0, "06:00"))


def _local_tz():
    return datetime.now().astimezone().tzinfo


def parse_when(text: str) -> tuple[str, int | None, str]:
    """``"daily 02:00"`` or ``"weekly sun 04:00"`` → (every, weekday, at)."""
    parts = text.strip().lower().split()
    if len(parts) == 2 and parts[0] == "daily":
        every, weekday, at = "daily", None, parts[1]
    elif len(parts) == 3 and parts[0] == "weekly" and parts[1][:3] in WEEKDAYS:
        every, weekday, at = "weekly", WEEKDAYS.index(parts[1][:3]), parts[2]
    else:
        raise JobRefused("when is 'daily HH:MM' or 'weekly DAY HH:MM' (mon … sun)")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise JobRefused(f"{at!r} is not a time HH:MM")
    return every, weekday, at


def next_due(every: str, weekday: int | None, at: str, after: datetime) -> datetime:
    """The first moment strictly after ``after`` the schedule names (local time), in UTC."""
    tz = _local_tz()
    local = after.astimezone(tz)
    hh, mm = (int(x) for x in at.split(":"))
    day: date = local.date()
    for _ in range(8):
        candidate = datetime.combine(day, time(hh, mm), tzinfo=tz)
        if candidate > local and (every == "daily" or candidate.weekday() == weekday):
            return candidate.astimezone(UTC)
        day += timedelta(days=1)
    raise ValueError("no next time")  # pragma: no cover - eight days always hold one


async def add_schedule(session: AsyncSession, data_source: str, kind: str, when: str, *,
                       created_by: str, params: dict[str, Any] | None = None,
                       now: datetime | None = None, known: set[str] | None = None
                       ) -> Schedule:
    """Create or replace the schedule of ``kind`` for ``data_source`` (one each)."""
    if kind not in SCHEDULED:
        raise JobRefused(f"a schedule's kind is one of {', '.join(SCHEDULED)}")
    if known is not None and data_source not in known:
        raise JobRefused(f"no data source {data_source!r}")
    every, weekday, at = parse_when(when)
    now = now or datetime.now(UTC)
    row = await session.scalar(select(Schedule).where(Schedule.data_source == data_source,
                                                      Schedule.kind == kind))
    if row is None:
        row = Schedule(data_source=data_source, kind=kind, created_by=created_by)
        session.add(row)
    row.every, row.weekday, row.at_time = every, weekday, at
    row.params, row.enabled, row.updated_at = dict(params or {}), True, now
    row.next_due_at = next_due(every, weekday, at, now)
    await session.flush()
    return row


async def enqueue_due(session: AsyncSession, *, now: datetime | None = None,
                      known: set[str] | None = None) -> list[tuple[Schedule, Any]]:
    """Queue every enabled schedule that is due (committed). Returns (schedule, the job or
    the reason it was not queued)."""
    now = now or datetime.now(UTC)
    due = (await session.scalars(select(Schedule).where(
        Schedule.enabled.is_(True), Schedule.next_due_at <= now)
        .order_by(Schedule.next_due_at).with_for_update(skip_locked=True))).all()
    out: list[tuple[Schedule, Any]] = []
    for row in due:
        try:
            job = await enqueue(session, row.kind, row.data_source, requested_by="schedule",
                                params=row.params, schedule_id=row.id, known=known)
            out.append((row, job))
        except JobRefused as exc:
            out.append((row, str(exc)))
        row.last_queued_at = now
        row.next_due_at = next_due(row.every, row.weekday, row.at_time, now)
    await session.commit()
    return out


def schedule_view(row: Schedule) -> dict[str, Any]:
    when = (f"daily {row.at_time}" if row.every == "daily"
            else f"weekly {WEEKDAYS[row.weekday or 0]} {row.at_time}")
    return {"id": str(row.id), "data_source": row.data_source, "kind": row.kind,
            "when": when, "params": row.params, "enabled": row.enabled,
            "next_due_at": row.next_due_at, "last_queued_at": row.last_queued_at,
            "created_by": row.created_by, "updated_at": row.updated_at}

