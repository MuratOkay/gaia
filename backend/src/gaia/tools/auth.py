"""Sign-in and access from the command line (Phase 5A, D68–D70).

The local account — setup and break-glass; the only account Gaia keeps a password for:

    make admin-user                                  # set its password (prompted twice)
    make admin-user ARGS="setup-code"                # a one-time code for the first sign-in
    make admin-user ARGS="status | disable | enable"

Directories, roles and grants (the admin screen does the same):

    make auth ARGS="directory add acme --settings ../infra/auth/acme.json"   # password prompted
    make auth ARGS="directory add corp --kind entra --settings ../infra/auth/entra-example.json"
    make auth ARGS="directory probe acme"            # "test connection"
    make auth ARGS="directory sync acme"             # every user and role, now
    make auth ARGS="directory list | roles acme | update acme --settings f.json | disable acme"
    make auth ARGS="grant admin --role acme/GAIA-Admins"
    make auth ARGS="grant sme --user acme/akaya --data-source demo_pg"
    make auth ARGS="grant pii_processor --role acme/GAIA-Finance --note 'KVKK list 2026-11'"
    make auth ARGS="grant category --role acme/GAIA-Finance --category Finance"   (5C)
    make auth ARGS="grants [--all] | revoke <grant id> | accounts"
    make auth ARGS="account relink acme/akaya"     # D78: the same person behind a new object
    make auth ARGS="account renew acme/akaya"      # D78: someone else — their own grants revoked
    make auth ARGS="account relink acme --all"     # a test domain provisioned again
    make auth ARGS="audit-check"                     # is the audit chain intact; its head
    make auth ARGS="refusals [--last 30]"            # why sign-ins were refused (D90)

A password is never an argument (it would stay in the shell history): it is prompted
for, or read from the environment variable named by ``--password-env``. The actor in the
audit is ``cli:<your OS user>`` — whoever can run this on the server.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.auth import audit
from gaia.auth.passwords import PasswordPolicyError
from gaia.auth.service import AuthError, AuthService, cli_actor
from gaia.core.config import get_settings
from gaia.datasources.crypto import CredentialError
from gaia.db.engine import dispose_engine, get_engine


def _secret(env: str | None, prompt: str, twice: bool = False) -> str:
    if env:
        value = os.environ.get(env)
        if not value:
            raise SystemExit(f"{env} is not set")
        return value
    value = getpass.getpass(f"{prompt} (not shown): ")
    if twice and getpass.getpass("again: ") != value:
        raise SystemExit("the two entries differ")
    return value


def _settings_arg(args: argparse.Namespace) -> dict | None:
    if getattr(args, "settings", None):
        return json.loads(Path(args.settings).read_text(encoding="utf-8"))
    if getattr(args, "settings_json", None):
        return json.loads(args.settings_json)
    return None


def _split(value: str, what: str) -> tuple[str | None, str]:
    """``acme/GAIA-Admins`` -> ("acme", "GAIA-Admins"); ``akaya`` -> (None, "akaya")."""
    if "/" in value:
        left, right = value.split("/", 1)
        return left, right
    if what == "role":
        raise SystemExit("a role is named with its directory: --role acme/GAIA-Admins")
    return None, value


def _print_directory(v: dict) -> None:
    print(f"  {v['name']:<16} {v['kind']:<6} {v['status']:<10} probed {v['probed_at'] or '-'}"
          f"  synced {v['synced_at'] or '-'}")
    for c in v.get("probe") or []:
        print(f"      [{'PASS' if c['ok'] else 'FAIL'}] {c['name']:<8} {c['detail']}")
    if v.get("sync"):
        print(f"      sync: {v['sync']}")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    svc = AuthService(settings, async_sessionmaker(get_engine(), expire_on_commit=False))
    actor = cli_actor()
    try:
        if args.command == "admin-user":
            action = args.action or "set"
            if action == "set":
                await svc.set_local_password(
                    _secret(args.password_env, "new password for gaia-admin", twice=True),
                    actor=actor)
                print("gaia-admin: password set; its open sessions are ended")
            elif action == "setup-code":
                code = await svc.issue_setup_code(actor=actor)
                print(f"one-time setup code for gaia-admin, valid "
                      f"{settings.auth.setup_code_minutes} min:\n\n  {code}\n\n"
                      "enter it on the sign-in screen (first sign-in) or POST /api/auth/setup")
            elif action in ("disable", "enable"):
                await svc.set_local_enabled(action == "enable", actor=actor)
                print(f"gaia-admin: {action}d")
            status = await svc.local_status()
            print(f"gaia-admin: {status}")
            return 0
        if args.command == "directory":
            a = args.action
            if a == "list":
                rows = await svc.directories()
                print(f"{len(rows)} directory(ies)")
                for v in rows:
                    _print_directory(v)
            elif a == "add":
                settings_doc = _settings_arg(args)
                if settings_doc is None:
                    raise SystemExit("--settings FILE.json (or --settings-json '…') is needed")
                what = "client secret" if args.kind == "entra" else "service account password"
                v = await svc.add_directory(args.name, args.kind, settings_doc,
                                            _secret(args.password_env, what), actor=actor)
                _print_directory(v)
                print(f"  next: make auth ARGS='directory probe {args.name}'")
            elif a == "update":
                v = await svc.update_directory(
                    args.name, actor=actor, settings=_settings_arg(args),
                    password=_secret(args.password_env, "service account password (Entra "
                                     "ID: the client secret's Value, not its ID)")
                    if args.password or args.password_env else None)
                _print_directory(v)
            elif a == "probe":
                v = await svc.probe_directory(args.name, actor=actor)
                _print_directory(v)
                if v.get("last_test_failed"):
                    print(f"  the test failed; {args.name} stays offered at sign-in (these "
                          "settings passed before), and sign-ins through it fail until it "
                          "passes again (D76)")
                ok = v["status"] == "verified" and not v.get("last_test_failed")
                return 0 if ok else 2
            elif a == "sync":
                _print_directory(await svc.sync_directory(args.name, actor=actor))
            elif a in ("disable", "enable"):
                _print_directory(await svc.set_directory_enabled(args.name, a == "enable",
                                                                 actor=actor))
            elif a == "roles":
                rows = await svc.roles(args.name, include_gone=args.gone)
                print(f"{len(rows)} role(s) in {args.name}")
                for r in rows:
                    gone = f"  gone {r['gone_at']:%Y-%m-%d}" if r["gone_at"] else ""
                    print(f"  {r['source']:<9} {r['name']:<32} members {r['members'] or '-':<5}"
                          f" {r['id']}{gone}")
            return 0
        if args.command == "grant":
            role = _split(args.role, "role") if args.role else None
            user = _split(args.user, "user") if args.user else None
            v = await svc.grant(args.privilege, actor=actor, role=role, account=user,
                                data_source=args.data_source, note=args.note,
                                category=args.category)
            print(f"granted {v['privilege']} to {v['subject']}"
                  + (f" on {v['data_source']}" if v["data_source"] else "")
                  + (f": {v['category']}" if v.get("category") else "") + f"  ({v['id']})")
            return 0
        if args.command == "revoke":
            v = await svc.revoke(uuid.UUID(args.grant_id), actor=actor, note=args.note)
            print(f"revoked {v['privilege']} of {v['subject']}")
            return 0
        if args.command == "grants":
            rows = await svc.grants(include_revoked=args.all)
            print(f"{len(rows)} grant(s)")
            for g in rows:
                end = f"  revoked {g['revoked_at']:%Y-%m-%d} by {g['revoked_by']}" \
                    if g["revoked_at"] else ""
                print(f"  {g['privilege']:<13} {g['subject']:<40} "
                      f"{g['data_source'] or g.get('category') or '':<12} by "
                      f"{g['granted_by']}{end}  {g['id']}")
            return 0
        if args.command == "accounts":
            rows = await svc.accounts()
            print(f"{len(rows)} account(s)")
            for r in rows:
                flags = ", ".join(r["roles"]) or "no Gaia role"
                if r["pii_processor"]:
                    flags += " + PII"
                if r["sme_sources"]:
                    flags += f" (sme: {', '.join(r['sme_sources'])})"
                if r.get("categories"):
                    flags += f" [reads: {', '.join(r['categories'])}]"
                state = "" if r["enabled"] else "  DISABLED"
                print(f"  {(r['directory'] or 'local') + '/' + r['login']:<32} {flags}{state}")
                if r.get("identity_changed_at"):
                    who = f"{r['directory']}/{r['login']}"
                    print(f"      IDENTITY CHANGED since {r['identity_changed_at']:%Y-%m-%d %H:%M}:"
                          " another directory object carries this login; sign-ins are refused"
                          f" — make auth ARGS=\"account relink {who}\" (the same person) or"
                          f" \"account renew {who}\" (someone else)")
                for c in r["conflicts"]:
                    print(f"      conflict: {c}")
            return 0
        if args.command == "account":
            directory, _, login = args.who.partition("/")
            if not login and not args.all:
                raise SystemExit("name the account: DIRECTORY/LOGIN (or DIRECTORY --all)")
            done = await svc.resolve_identity(directory, login or None, args.action,
                                              actor=actor, note=args.note)
            for d in done:
                print(f"{d['action']}ed {d['directory']}/{d['login']}"
                      + (f"; {d['grants_revoked']} own grant(s) revoked"
                         if d["action"] == "renew" else ""))
            return 0
        if args.command == "refusals":  # D90: why nobody could sign in (the screen won't say)
            from sqlalchemy import select

            from gaia.db.models.audit import AuthEvent

            async with svc.factory() as s:
                rows = (await s.scalars(select(AuthEvent).where(AuthEvent.action.in_(
                    ("sign_in_refused", "identity_changed", "sign_in")))
                    .order_by(AuthEvent.seq.desc()).limit(args.last))).all()
            if not rows:
                print("no sign-in recorded yet")
            for e in reversed(rows):
                # identity_changed is noticed once, beside the refusal of the same attempt
                what = {"sign_in": "signed in", "identity_changed": "NOTICED — this login's "
                        "directory object is a new one (D78): make auth ARGS=\"account relink "
                        f"{e.subject or 'DIR'}/{e.actor or 'LOGIN'}\" if it is the same person, "
                        "\"account relink "
                        f"{e.subject or 'DIR'} --all\" after a directory built anew"
                        }.get(e.action)
                reason = what or f"REFUSED — {(e.detail or {}).get('reason', '?')}"
                print(f"  {e.at:%Y-%m-%d %H:%M:%S}  {e.actor or '—':<24} "
                      f"{e.subject or '—':<10} {reason}")
            return 0
        if args.command == "audit-check":
            async with svc.factory() as s:
                check = await audit.verify_chain(s)
            print(f"audit chain: {'intact' if check.ok else 'BROKEN'} — {check.events} event(s); "
                  f"head {check.head}")
            if not check.ok:
                print(f"  first bad row: seq {check.broken_at} ({check.reason})")
            return 0 if check.ok else 2
    except (AuthError, CredentialError, PasswordPolicyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign-in and access (Phase 5A).")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("admin-user", help="the one local account (gaia-admin)")
    s.add_argument("action", nargs="?", choices=["set", "setup-code", "status", "disable",
                                                 "enable"])
    s.add_argument("--password-env", help="read the new password from this variable")

    s = sub.add_parser("directory", help="company directories (AD/LDAP)")
    s.add_argument("action", choices=["list", "add", "update", "probe", "sync", "disable",
                                      "enable", "roles"])
    s.add_argument("name", nargs="?")
    s.add_argument("--kind", default="ldap", choices=["ldap", "entra"])
    s.add_argument("--settings", help="a JSON file with the directory's settings")
    s.add_argument("--settings-json", help="the settings as a JSON string")
    s.add_argument("--password", action="store_true", help="change the service password")
    s.add_argument("--password-env", help="read the service password from this variable")
    s.add_argument("--gone", action="store_true", help="roles: also those no longer there")

    s = sub.add_parser("grant", help="give a Gaia role or permission")
    s.add_argument("privilege", choices=["user", "sme", "admin", "auditor", "pii_processor",
                                         "category"])
    who = s.add_mutually_exclusive_group(required=True)
    who.add_argument("--role", help="a directory role: DIRECTORY/NAME")
    who.add_argument("--user", help="an account: [DIRECTORY/]LOGIN")
    s.add_argument("--data-source", help="for sme: the data source reviewed")
    s.add_argument("--category", help="for category: the data category (make access)")
    s.add_argument("--note")

    s = sub.add_parser("revoke", help="end a grant (kept, dated and signed)")
    s.add_argument("grant_id")
    s.add_argument("--note")

    s = sub.add_parser("grants", help="current grants")
    s.add_argument("--all", action="store_true", help="revoked ones too")
    sub.add_parser("accounts", help="accounts and what they may do")
    s = sub.add_parser("account", help="D78: decide about an account whose directory object "
                                       "changed")
    s.add_argument("action", choices=["relink", "renew"])
    s.add_argument("who", help="DIRECTORY/LOGIN, or DIRECTORY with --all")
    s.add_argument("--all", action="store_true", help="every account of the directory waiting")
    s.add_argument("--note")
    sub.add_parser("audit-check", help="verify the hash-chained audit")
    s = sub.add_parser("refusals", help="the last sign-ins and refusals, with the reason the "
                                        "sign-in screen does not show")
    s.add_argument("--last", type=int, default=30)

    args = parser.parse_args(argv)
    if args.command == "directory" and args.action not in ("list",) and not args.name:
        parser.error("name the directory")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
