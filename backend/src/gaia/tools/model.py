"""Models as managed things (Phase 7D; P7-4, P7-5; D101).

    make model                                               # the models and what each serves
    make model ARGS="add qwen --base-url https://gw/v1 --model-id qwen-max --key-env QWEN_KEY"
    make model ARGS="add local --base-url http://localhost:11434/v1 --model-id llama3 --key \\
                     --locality local"                       # the key is prompted, stored encrypted
    make model ARGS="probe qwen"                             # plain, JSON mode, thinking
    make model TARGET=demo_pg ARGS="trial qwen"              # a job: answering and meanings
    make model TARGET=demo_pg ARGS="trial qwen --purpose answering --param runs=3"
    make model ARGS="show"                                   # the last trial (or: show JOB)
    make model ARGS="switch qwen --purpose answering"        # needs a trial of it (or --force)
    make model ARGS="rollback --purpose answering"           # back to the model it replaced
    make model ARGS="retire qwen"

The model ``config/gaia.yaml`` names is always listed (``config``); the others are registered
here or on the *Models* page. A trial runs in the worker (``make worker``). Every addition,
switch, rollback and retirement is audited; a switch also raises an alert for every
administrator. See ``gaia.models.registry`` and ``gaia.models.trial``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth import audit
from gaia.auth.service import cli_actor
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.models.ops import MODEL_PURPOSES, Job
from gaia.jobs.queue import JobRefused, alert, enqueue
from gaia.jobs.worker import known_data_sources
from gaia.models.registry import (
    ModelError,
    add_model,
    assignments,
    get_model,
    list_models,
    meanings_by_model,
    probe_model,
    retire,
    rollback,
    switch,
)
from gaia.models.trial import trial_lines
from gaia.tools.jobs import parse_params


async def _trials(session) -> list[Job]:
    return list((await session.scalars(select(Job).where(Job.kind == "model_trial")
                                       .order_by(Job.created_at.desc()).limit(20))).all())


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    actor = cli_actor()
    label = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    cmd = args.command or "list"
    try:
        async with factory() as s:
            if cmd == "list":
                serving = await assignments(s)
                served = {p: (serving[p].model_name if p in serving else settings.llm.model)
                          for p in MODEL_PURPOSES}
                trials = await _trials(s)
                for m in await list_models(s, settings, include_retired=args.all):
                    uses = [p for p, n in served.items() if n == m.name]
                    last = next((j for j in trials if (j.params or {}).get("model") == m.name),
                                None)
                    print(f"  {m.name:<22} {m.origin:<6} {m.status:<10} {m.model_id:<28} "
                          f"{m.locality:<6} {('serves ' + ', '.join(uses)) if uses else ''}")
                    if last is not None:
                        print(f"      last trial {str(last.id)[:8]} {last.state}"
                              f" on {last.data_source} ({last.created_at:%Y-%m-%d})")
                for ds, counts in (await meanings_by_model(s)).items():
                    print(f"  meanings of {ds}: " + ", ".join(f"{m} {n}"
                                                               for m, n in counts.items()))
                for p in MODEL_PURPOSES:
                    row = serving.get(p)
                    if row is not None and row.previous:
                        print(f"  {p}: {row.model_name} since {row.since:%Y-%m-%d %H:%M}, "
                              f"replacing {row.previous}"
                              + (f" — rollback possible until {row.rollback_until:%Y-%m-%d}"
                                 if row.rollback_until else ""))
                return 0
            if cmd == "add":
                key = getpass.getpass("key (not shown): ") if args.key else None
                options = parse_params(args.option)
                entry = await add_model(s, settings, name=args.name, base_url=args.base_url,
                                        model_id=args.model_id, key=key, key_env=args.key_env,
                                        locality=args.locality, options=options, by=actor)
                await audit.record(s, "model_added", actor=actor, subject=entry.name,
                                   detail={"model_id": entry.model_id,
                                           "base_url": entry.base_url,
                                           "locality": entry.locality})
                await s.commit()
                print(f"registered {entry.name} (unverified): `make model ARGS=\"probe "
                      f"{entry.name}\"`")
                return 0
            if cmd == "probe":
                entry = await probe_model(s, settings, args.name)
                await audit.record(s, "model_probed", actor=actor, subject=entry.name,
                                   detail={"status": entry.status})
                await s.commit()
                for c in entry.probe or []:
                    print(f"  {'ok  ' if c['ok'] else 'FAIL'} {c['name']:<9} {c['detail']} "
                          f"({c['seconds']} s)")
                print(f"{entry.name}: {entry.status}")
                return 0 if entry.status == "verified" else 1
            if cmd == "trial":
                entry = await get_model(s, settings, args.name)
                if not entry.usable:
                    raise ModelError(f"{entry.name} is {entry.status}: probe it first")
                params = {**parse_params(args.param), "model": args.name,
                          "purpose": args.purpose}
                job = await enqueue(s, "model_trial", label, requested_by=actor, params=params,
                                    known=await known_data_sources(s, settings))
                await audit.record(s, "job_started", actor=actor,
                                   subject=f"{label}: model_trial",
                                   detail={"job": str(job.id), "params": params})
                await s.commit()
                print(f"queued a trial of {args.name} on {label}: job {str(job.id)[:8]} — the "
                      "worker runs it (`make model ARGS=\"show\"` when done)")
                return 0
            if cmd == "show":
                trials = await _trials(s)
                job = next((j for j in trials if not args.id
                            or str(j.id).startswith(args.id.lower())), None)
                if job is None:
                    print("no trial yet")
                    return 1
                print(f"job {str(job.id)[:8]} on {job.data_source}: {job.state}"
                      + (f" — {job.error}" if job.error else ""))
                for line in trial_lines(job.result or {}):
                    print(line)
                return 0
            if cmd in ("switch", "rollback"):
                if cmd == "switch":
                    row = await switch(s, settings, args.purpose, args.name, by=actor,
                                       force=args.force, note=args.note)
                else:
                    row = await rollback(s, settings, args.purpose, by=actor, note=args.note)
                await audit.record(s, f"model_{'switched' if cmd == 'switch' else 'rolled_back'}",
                                   actor=actor, subject=args.purpose,
                                   detail={"model": row.model_name, "previous": row.previous,
                                           "forced": row.forced, "note": args.note})
                await alert(s, "model_switched",
                            f"{args.purpose} is now served by {row.model_name} (was "
                            f"{row.previous}, by {actor})" + (" — without a trial (forced)"
                                                                if row.forced else ""),
                            detail={"purpose": args.purpose, "model": row.model_name,
                                    "previous": row.previous})
                await s.commit()
                print(f"{args.purpose}: {row.model_name} from now on (was {row.previous})"
                      + (f"; rollback possible until {row.rollback_until:%Y-%m-%d}"
                         if row.rollback_until else ""))
                if args.purpose == "reasoning":
                    print("the stored meanings are re-reasoned by the nightly refresh, a part "
                          "each night within its token budget; SME decisions stay")
                return 0
            if cmd == "retire":
                entry = await retire(s, settings, args.name, by=actor)
                await audit.record(s, "model_retired", actor=actor, subject=entry.name)
                await s.commit()
                print(f"{entry.name}: retired (its key wiped; its meanings and runs stay)")
                return 0
    except (ModelError, JobRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Models as managed things (7D).")
    p.add_argument("--target")
    sub = p.add_subparsers(dest="command")
    x = sub.add_parser("list")
    x.add_argument("--all", action="store_true", help="retired models too")
    x = sub.add_parser("add")
    x.add_argument("name")
    x.add_argument("--base-url", required=True)
    x.add_argument("--model-id", required=True)
    x.add_argument("--key-env", help="the environment variable holding the key")
    x.add_argument("--key", action="store_true", help="type the key (stored encrypted)")
    x.add_argument("--locality", default="remote", choices=["remote", "local"])
    x.add_argument("--option", action="append", help="key=value (concurrency, timeout_s, …)")
    x = sub.add_parser("probe")
    x.add_argument("name")
    x = sub.add_parser("trial")
    x.add_argument("name")
    x.add_argument("--purpose", default="both", choices=["answering", "reasoning", "both"])
    x.add_argument("--param", action="append", help="runs=3, tables=8, file=…")
    x = sub.add_parser("show")
    x.add_argument("id", nargs="?")
    x = sub.add_parser("switch")
    x.add_argument("name")
    x.add_argument("--purpose", required=True, choices=list(MODEL_PURPOSES))
    x.add_argument("--force", action="store_true", help="without a recent trial (recorded)")
    x.add_argument("--note")
    x = sub.add_parser("rollback")
    x.add_argument("--purpose", required=True, choices=list(MODEL_PURPOSES))
    x.add_argument("--note")
    x = sub.add_parser("retire")
    x.add_argument("name")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
