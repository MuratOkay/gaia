"""Jobs, schedules and alerts from the command line (Phase 7C; D98).

    make jobs                                             # the last 20 jobs
    make jobs ARGS="show 1a2b3c4d"                        # one job, with its log
    make jobs TARGET=demo_pg ARGS="start refresh"         # queue one now (the worker runs it)
    make jobs TARGET=demo_pg ARGS="start refresh --param budget=500000 --param ignore_window=1"
    make jobs TARGET=demo_pg ARGS="start drift_watch"     # the golden set, three fresh runs
    make jobs TARGET=demo_pg ARGS="start sample"          # the week's calibration sample
    make jobs ARGS="cancel 1a2b3c4d"
    make jobs ARGS="schedules"
    make jobs TARGET=demo_pg ARGS="schedule defaults"     # P7-7: refresh nightly, watch weekly
    make jobs TARGET=demo_pg ARGS="schedule refresh daily 02:00"
    make jobs TARGET=demo_pg ARGS="schedule drift_watch weekly sun 04:00"
    make jobs TARGET=demo_pg ARGS="schedule off refresh"  # or "schedule on refresh"
    make jobs ARGS="alerts"                               # what to look at; "ack ID" when done

A queued job runs when ``make worker`` runs (it also queues what the schedules say is due).
Starting, cancelling, scheduling and acknowledging are audited with the operator's login
(``cli:<user>``), as on the *Runs* page.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth import audit
from gaia.auth.service import cli_actor
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.models.ops import Alert, Job, JobEvent, Schedule
from gaia.jobs.queue import (
    JobNotFound,
    JobRefused,
    acknowledge,
    enqueue,
    open_alerts,
    request_cancel,
)
from gaia.jobs.schedules import DEFAULTS, WEEKDAYS, add_schedule, schedule_view
from gaia.jobs.worker import known_data_sources

__all__ = ["main", "parse_params"]


def parse_params(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise JobRefused(f"--param {item!r}: give key=value")
        v: Any = value.strip()
        if v.lower() in ("1", "true", "yes"):
            v = True
        elif v.lower() in ("0", "false", "no"):
            v = False
        elif v.isdigit():
            v = int(v)
        out[key.strip()] = v
    return out


async def _find(session, model, ref: str):
    ref = ref.strip().lower()
    if len(ref) < 6:
        raise JobRefused("give the id, or at least its first 6 characters")
    rows = (await session.scalars(select(model).where(
        cast(model.id, Text).like(f"{ref}%")))).all()
    if not rows:
        raise JobNotFound(f"no {model.__tablename__} {ref!r}")
    if len(rows) > 1:
        raise JobRefused(f"{ref!r} is more than one: give more of the id")
    return rows[0]


def _line(j: Job) -> str:
    step = (j.progress or {}).get("step")
    return (f"  {str(j.id)[:8]}  {j.created_at:%Y-%m-%d %H:%M}  {j.data_source:<12} "
            f"{j.kind:<12} {j.state:<9} {j.requested_by:<14} {j.tokens:>10,} tok"
            + (f"  [{step}]" if j.state == "running" and step else "")
            + (f"  {j.error[:70]}" if j.error else ""))


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    actor = cli_actor()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    label = args.target or settings.app.default_target
    try:
        async with factory() as s:
            known = await known_data_sources(s, settings)
            cmd = args.command or "list"
            if cmd == "list":
                q = select(Job).order_by(Job.created_at.desc()).limit(args.last)
                if args.target:
                    q = q.where(Job.data_source == args.target)
                rows = (await s.scalars(q)).all()
                if not rows:
                    print("no job yet: `make jobs TARGET=… ARGS=\"start refresh\"`, then "
                          "`make worker`")
                for j in reversed(rows):
                    print(_line(j))
                return 0
            if cmd == "show":
                j = await _find(s, Job, args.id)
                print(_line(j))
                for key in ("params", "result", "progress"):
                    if getattr(j, key):
                        print(f"  {key}: {getattr(j, key)}")
                for e in (await s.scalars(select(JobEvent).where(JobEvent.job_id == j.id)
                                          .order_by(JobEvent.seq))).all():
                    print(f"    {e.at:%H:%M:%S} {e.level:<5} {e.message}")
                return 0
            if cmd == "start":
                params = parse_params(args.param)
                job = await enqueue(s, args.kind, label, requested_by=actor, params=params,
                                    known=known)
                await audit.record(s, "job_started", actor=actor, subject=f"{label}: {args.kind}",
                                   detail={"job": str(job.id), "params": params})
                await s.commit()
                print(f"queued {args.kind} of {label}: job {str(job.id)[:8]} — `make worker` "
                      "runs it")
                return 0
            if cmd == "cancel":
                j = await _find(s, Job, args.id)
                j = await request_cancel(s, j.id, by=actor)
                await audit.record(s, "job_cancelled", actor=actor,
                                   subject=f"{j.data_source}: {j.kind}", detail={"job": str(j.id)})
                await s.commit()
                print(f"{str(j.id)[:8]}: " + ("cancelled" if j.state == "cancelled" else
                                               "asked to stop at its next table"))
                return 0
            if cmd == "schedules":
                rows = (await s.scalars(select(Schedule).order_by(Schedule.data_source,
                                                                  Schedule.kind))).all()
                if not rows:
                    print("no schedule: `make jobs TARGET=… ARGS=\"schedule defaults\"`")
                for r in rows:
                    v = schedule_view(r)
                    due = f"{v['next_due_at']:%Y-%m-%d %H:%M}"
                    print(f"  {v['data_source']:<12} {v['kind']:<12} {v['when']:<20} "
                          f"{'on ' if v['enabled'] else 'OFF'} next {due} UTC"
                          + (f"  {v['params']}" if v["params"] else ""))
                return 0
            if cmd == "schedule":
                words = args.words
                if not words:
                    raise JobRefused("schedule defaults | KIND WHEN… | off KIND | on KIND")
                if words[0] == "defaults":
                    window = (settings.target(label).profiling_window
                              if label in settings.targets else None) or settings.profiling.window
                    for kind, every, weekday, at in DEFAULTS:
                        if kind == "refresh" and window:
                            at = window.split("-", 1)[0]  # nightly, as the window opens
                        when = (f"daily {at}" if every == "daily"
                                else f"weekly {WEEKDAYS[weekday]} {at}")
                        row = await add_schedule(s, label, kind, when, created_by=actor,
                                                 known=known)
                        print(f"  {label} {kind}: {schedule_view(row)['when']}")
                    changed = {"defaults": True}
                elif words[0] in ("on", "off"):
                    if len(words) != 2:
                        raise JobRefused("schedule on KIND | off KIND")
                    row = await s.scalar(select(Schedule).where(
                        Schedule.data_source == label, Schedule.kind == words[1]))
                    if row is None:
                        raise JobNotFound(f"no {words[1]} schedule for {label}")
                    row.enabled = words[0] == "on"
                    changed = {"kind": words[1], "enabled": row.enabled}
                    print(f"{label} {words[1]}: {'on' if row.enabled else 'off'}")
                else:
                    params = parse_params(args.param)
                    row = await add_schedule(s, label, words[0], " ".join(words[1:]),
                                             created_by=actor, params=params, known=known)
                    changed = {"kind": words[0], "when": schedule_view(row)["when"],
                               "params": params}
                    print(f"{label} {words[0]}: {schedule_view(row)['when']}, next "
                          f"{row.next_due_at:%Y-%m-%d %H:%M} UTC")
                await audit.record(s, "schedule_changed", actor=actor, subject=label,
                                   detail=changed)
                await s.commit()
                return 0
            if cmd == "alerts":
                rows = await open_alerts(s)
                if not rows:
                    print("no open alert")
                for a in rows:
                    print(f"  {str(a.id)[:8]}  {a.created_at:%Y-%m-%d %H:%M}  {a.kind:<14} "
                          f"{a.message}")
                return 0
            if cmd == "ack":
                a = await _find(s, Alert, args.id)
                await acknowledge(s, a.id, by=actor)
                await audit.record(s, "alert_acknowledged", actor=actor, subject=a.kind,
                                   detail={"alert": str(a.id)})
                await s.commit()
                print(f"acknowledged {str(a.id)[:8]}")
                return 0
    except (JobRefused, JobNotFound) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Jobs, schedules and alerts (7C).")
    p.add_argument("--target")
    p.add_argument("--last", type=int, default=20)
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list")
    x = sub.add_parser("show")
    x.add_argument("id")
    x = sub.add_parser("start")
    x.add_argument("kind")
    x.add_argument("--param", action="append", help="key=value (repeatable)")
    x = sub.add_parser("cancel")
    x.add_argument("id")
    sub.add_parser("schedules")
    x = sub.add_parser("schedule")
    x.add_argument("words", nargs="*")
    x.add_argument("--param", action="append", help="key=value (repeatable)")
    sub.add_parser("alerts")
    x = sub.add_parser("ack")
    x.add_argument("id")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
