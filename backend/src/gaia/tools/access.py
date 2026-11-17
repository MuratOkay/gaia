"""Data categories from the command line (Phase 5C, D74) — the admin screen does the same.

    make access ARGS="categories [--all]"
    make access ARGS="add Finance --keywords finance,invoice --description 'money'"
    make access ARGS="change Finance --keywords finance,invoice,ledger | retire Finance"
    make access TARGET=demo_pg ARGS="from-schemas"          # one category per schema
    make access TARGET=demo_pg ARGS="propose"               # Gaia's proposals
    make access TARGET=demo_pg ARGS="objects [--why]"       # every object, its category (and why)
    make access TARGET=demo_pg ARGS="place finance.invoices Finance --note 'SME said so'"
    make access ARGS="suggestions"                          # directory roles -> categories
    make access TARGET=demo_pg ARGS="policy akaya [--directory acme]"   # what they may read

Granting a category: ``make auth ARGS="grant category --role acme/GAIA-Finance --category
Finance"``. Every change is audited with ``cli:<your OS user>`` as the actor.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.access.categories import CategoryError, CategoryService
from gaia.auth.service import cli_actor
from gaia.core.config import get_settings
from gaia.db.engine import dispose_engine, get_engine


def _words(text: str | None) -> list[str] | None:
    return None if text is None else [w.strip() for w in text.split(",") if w.strip()]


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = args.target or settings.app.default_target
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    svc = CategoryService(factory)
    actor = cli_actor()
    try:
        c = args.command
        if c == "categories":
            rows = await svc.categories(include_retired=args.all)
            print(f"{len(rows)} categor{'y' if len(rows) == 1 else 'ies'}")
            for r in rows:
                gone = "  RETIRED" if r["retired_at"] else ""
                print(f"  {r['name']:<20} objects {r['objects']:<4} grants {r['grants']:<3} "
                      f"keywords: {', '.join(r['keywords']) or '-'}{gone}")
        elif c == "add":
            r = await svc.add(args.name, actor=actor, description=args.description,
                              keywords=_words(args.keywords))
            print(f"added {r['name']} ({r['id']})")
        elif c == "change":
            r = await svc.change(args.name, actor=actor, name=args.new_name,
                                 description=args.description, keywords=_words(args.keywords))
            print(f"changed {r['name']}")
        elif c == "retire":
            r = await svc.retire(args.name, actor=actor)
            print(f"retired {r['name']}")
        elif c == "from-schemas":
            made = await svc.from_schemas(target, actor=actor)
            added = [m for m in made if m.get("action") == "added"]
            completed = [m for m in made if m.get("action") == "completed"]
            print(f"{len(added)} categor{'y' if len(added) == 1 else 'ies'} added: "
                  + (", ".join(f"{m['name']} ({', '.join(m['keywords'])})" for m in added)
                     or "—"))
            for m in completed:
                print(f"  {m['name']}: keywords added {', '.join(m['added_keywords'])}")
        elif c == "propose":
            r = await svc.propose(target, actor=actor)
            print(f"{target}: {r['proposed']} proposed, {r['unchanged']} unchanged, "
                  f"{r['confirmed_kept']} confirmed kept, {r['none']} without a proposal")
            bases = r.get("by_basis") or {}
            if bases:
                print("  proposals rest on: " + ", ".join(
                    f"{n} {b}" for b, n in sorted(bases.items()))
                      + ("  — `objects` lists them; check the weak and related ones"
                         if bases.get("weak") or bases.get("related") else ""))
            for name, why in sorted(r["why_none"].items()):
                print(f"  no proposal for {name}: {why}")
        elif c == "objects":
            rows = await svc.objects(target)
            missing = sum(1 for r in rows if r["category"] is None)
            print(f"{target}: {len(rows)} object(s), {missing} without a category "
                  "(readable by nobody)")
            for r in rows:
                cat = r["category"]
                place = "—" if cat is None else (
                    f"{cat['name']} ({cat['status']} by {cat['decided_by'] or 'gaia'}"
                    + (f", {cat['basis']}" if cat.get("basis") else "") + ")")
                print(f"  {r['name']:<40} {r['kind']:<6} {place}")
                if args.why and cat and cat["status"] == "proposed" and cat.get("reason"):
                    print(f"  {'':<40}        {cat['reason']}")
        elif c == "place":
            from gaia.db.models.catalog import DataSource, TableRecord

            schema, _, name = args.object.partition(".")
            async with factory() as s:
                table_id = await s.scalar(select(TableRecord.id).join(
                    DataSource, DataSource.id == TableRecord.data_source_id).where(
                    DataSource.name == target, TableRecord.schema_name == schema,
                    TableRecord.object_name == name))
            if table_id is None:
                raise CategoryError(f"no object {args.object} in {target}")
            r = await svc.assign(target, table_id, args.category, actor=actor, source="admin",
                                 note=args.note)
            print(f"{r['object']}: {r['category']} (confirmed)")
        elif c == "suggestions":
            for r in await svc.role_suggestions():
                print(f"  {r['directory']}/{r['role']:<30} -> {', '.join(r['categories'])}")
        elif c == "policy":
            from gaia.access.policy import policy_for_login
            from gaia.query.retrieval_store import load_retrieval_index

            async with factory() as s:
                index = await load_retrieval_index(s, target, settings)
                policy = await policy_for_login(s, index, args.login, args.directory)
            total = len(index.objects)
            print(f"{policy.label} on {target}: {len(policy.readable)} of {total} object(s) "
                  f"readable; personal data {'unmasked' if policy.pii_processor else 'masked'}")
            for key in sorted(policy.readable):
                cols = policy.readable[key]
                all_cols = len(index.obj(key).columns) if index.obj(key) else len(cols)
                part = "" if len(cols) == all_cols else f"  ({len(cols)} of {all_cols} columns)"
                print(f"  {policy.objects.get(key, key)}{part}")
        return 0
    except (CategoryError, LookupError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Data categories (need-to-know).")
    p.add_argument("--target", help="data source (default: app.default_target)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("categories")
    s.add_argument("--all", action="store_true", help="retired ones too")
    s = sub.add_parser("add")
    s.add_argument("name")
    s.add_argument("--keywords", help="comma-separated")
    s.add_argument("--description")
    s = sub.add_parser("change")
    s.add_argument("name")
    s.add_argument("--name", dest="new_name")
    s.add_argument("--keywords")
    s.add_argument("--description")
    s = sub.add_parser("retire")
    s.add_argument("name")
    sub.add_parser("from-schemas")
    sub.add_parser("propose")
    o = sub.add_parser("objects")
    o.add_argument("--why", action="store_true", help="the reasons of Gaia's proposals")
    s = sub.add_parser("place")
    s.add_argument("object", help="schema.object")
    s.add_argument("category")
    s.add_argument("--note")
    sub.add_parser("suggestions")
    s = sub.add_parser("policy")
    s.add_argument("login")
    s.add_argument("--directory")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

