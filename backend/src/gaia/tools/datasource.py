"""Data sources added at runtime (Phase 4F; the admin path until the API has a login).

    make master-key                                    # a new GAIA_MASTER_KEY for .env
    make datasource ARGS="list"
    make datasource ARGS="add --name sales_pg --dialect postgres --host 10.0.0.5 --port 5432 \\
                          --database sales --user gaia_ro"         # the password is prompted
    make datasource ARGS="probe sales_pg"               # the read-only probe; --all for all
    make datasource ARGS="update sales_pg --host 10.0.0.6"           # --password to change it
    make datasource ARGS="disable sales_pg" | enable | remove
    make datasource ARGS="rotate-key"                   # after a new GAIA_MASTER_KEY

A data source added here is usable by every command (``TARGET=sales_pg``) once its
probe passed (``verified``), for ``datasources.reprobe_days``. The password is never
an argument (it would stay in the shell history): it is prompted for, or read from
the environment variable named by ``--password-env``. It is encrypted before it is
stored and never printed. Targets of ``config/gaia.yaml`` are listed too; they are
changed there.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import get_settings
from gaia.datasources.crypto import CredentialError, new_key
from gaia.datasources.service import DataSourceError, DataSourceService, DataSourceSpec
from gaia.db.engine import dispose_engine, get_engine


def _password(args: argparse.Namespace, required: bool) -> str | None:
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if not value:
            raise SystemExit(f"{args.password_env} is not set")
        return value
    if required or getattr(args, "password", False):
        return getpass.getpass("password (not shown): ")
    return None


def _pairs(items: list[str] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in items or []:
        key, _, value = item.partition("=")
        if not key or not _:
            raise SystemExit(f"expected key=value, not {item!r}")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def _print(view: dict) -> None:
    probe = view.get("probe") or []
    failed = [c["name"] for c in probe if not c["ok"]]
    when = view["probed_at"].isoformat(timespec="seconds") if view.get("probed_at") else "never"
    print(f"  {view['name']:<24} {view['origin']:<7} {view['status']:<11} {view['dialect']:<8} "
          f"{view.get('username') or ''}@{view.get('host') or ''}:{view.get('port') or ''}/"
          f"{view.get('database') or ''}  probed {when}"
          + (f"  failed: {', '.join(failed)}" if failed else ""))
    if view.get("status") in ("failed", "verified") and probe:
        for c in probe:
            print(f"      [{'PASS' if c['ok'] else 'FAIL'}] {c['name']:<20} {c['detail'][:100]}")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            svc = DataSourceService(session, settings, actor=os.environ.get("USER"))
            if args.command == "list":
                views = await svc.list(include_removed=args.removed)
                print(f"{len(views)} data source(s)")
                for v in views:
                    _print(v)
                return 0
            if args.command == "add":
                spec = DataSourceSpec(
                    name=args.name, dialect=args.dialect, host=args.host, port=args.port,
                    database=args.database, username=args.user, options=_pairs(args.option),
                    limits=_pairs(args.limit))
                view = await svc.add(spec, args.secret or "")
                await session.commit()
                print(f"added {view['name']} (unverified): run "
                      f"make datasource ARGS='probe {view['name']}'")
                return 0
            if args.command == "probe":
                names = [v["name"] for v in await svc.list() if v["origin"] == "admin"
                         and v["status"] != "disabled"] if args.all else [args.name]
                worst = 0
                for name in names:
                    view = await svc.probe(name)
                    await session.commit()
                    _print(view)
                    worst = worst or (0 if view["status"] == "verified" else 2)
                return worst
            if args.command == "update":
                changes = {k: v for k, v in {
                    "host": args.host, "port": args.port, "database": args.database,
                    "username": args.user}.items() if v is not None}
                if args.option:
                    changes["options"] = _pairs(args.option)
                if args.limit:
                    changes["limits"] = _pairs(args.limit)
                view = await svc.update(args.name, changes, args.secret)
                await session.commit()
                _print(view)
                if view["status"] == "unverified":
                    print(f"  changed: probe it again (make datasource ARGS='probe {args.name}')")
                return 0
            if args.command in ("enable", "disable"):
                view = await svc.set_enabled(args.name, args.command == "enable")
                await session.commit()
                _print(view)
                return 0
            if args.command == "remove":
                view = await svc.remove(args.name)
                await session.commit()
                print(f"removed: kept as {view['name']} (tombstoned), credential deleted")
                return 0
            if args.command == "rotate-key":
                n = await svc.rotate()
                await session.commit()
                print(f"re-encrypted: {n} credential(s) moved to the current key")
                return 0
    except (DataSourceError, CredentialError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Data sources added at runtime (Phase 4F).")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("list")
    s.add_argument("--removed", action="store_true")
    s = sub.add_parser("add")
    s.add_argument("--name", required=True)
    s.add_argument("--dialect", required=True, choices=["postgres", "postgresql", "mssql"])
    s.add_argument("--host", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--database", required=True)
    s.add_argument("--user", required=True)
    s.add_argument("--option", action="append", help="key=value, e.g. sslmode=require")
    s.add_argument("--limit", action="append", help="key=value, e.g. statement_timeout_s=30")
    s.add_argument("--password-env", help="read the password from this environment variable")
    s = sub.add_parser("probe")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("update")
    s.add_argument("name")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--database")
    s.add_argument("--user")
    s.add_argument("--option", action="append")
    s.add_argument("--limit", action="append")
    s.add_argument("--password", action="store_true", help="change the password (prompted)")
    s.add_argument("--password-env")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("name")
    s = sub.add_parser("remove")
    s.add_argument("name")
    s.add_argument("--yes", action="store_true")
    sub.add_parser("rotate-key")
    sub.add_parser("new-key", help="print a new master key for .env")
    args = p.parse_args(argv)
    if args.command == "new-key":
        print(f"GAIA_MASTER_KEY={new_key()}")
        return 0
    if args.command == "probe" and not (args.name or args.all):
        p.error("probe NAME, or probe --all")
    # prompts before the event loop: nothing blocks it
    args.secret = _password(args, required=args.command == "add") \
        if args.command in ("add", "update") else None
    if args.command == "remove" and not args.yes:
        answer = input(f"remove {args.name}? its credential is deleted [y/N] ")
        if answer.strip().lower() != "y":
            print("aborted")
            return 1
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
