"""The worker (Phase 7C; D98): runs queued and scheduled jobs.

    make worker                  # until stopped (Ctrl-C: the running job stops at its next table)
    make worker ARGS="--once"    # queue what is due, run everything queued, then exit (cron)

One per installation (P7-6). It queues what the schedules say is due, marks jobs whose worker
went silent as failed, and runs the oldest queued job, one at a time — see
``gaia.jobs.worker``. What it does is in the job's log (``make jobs ARGS="show ID"``, *Runs*).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.jobs.worker import Worker


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    def echo(message: str) -> None:
        print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}  {message}", flush=True)

    worker = Worker(settings, factory, echo=echo)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def shutdown() -> None:
        if not stop.is_set():
            echo("stopping: the running job stops at its next table")
        stop.set()
        worker.stop_current()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except (NotImplementedError, RuntimeError):  # not on this platform
            pass
    echo(f"worker {worker.name}: polling every {settings.jobs.poll_seconds:g} s"
         + (" (once)" if args.once else ""))
    try:
        ran = await worker.loop(stop, once=args.once)
    finally:
        await dispose_engine()
    echo(f"worker {worker.name}: {ran} job(s) run")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run queued and scheduled jobs (7C).")
    p.add_argument("--once", action="store_true",
                   help="queue what is due, run everything queued, then exit")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
