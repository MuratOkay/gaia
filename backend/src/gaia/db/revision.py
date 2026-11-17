"""Is the metadata store's schema the one this build expects? (D76)

On the owner's host the §22e commands ran on build D75 before its migration (``0015``) was
applied: nine commands ended in 900 lines of ``UndefinedColumnError`` tracebacks and the
grants silently did not happen. Now ``/health`` reports the schema revision, the API logs
an error at start-up, and a command-line tool that fails on a missing table or column says
in one line what to run.
"""

from __future__ import annotations

import re
from functools import lru_cache

from gaia.core.config import ROOT

__all__ = ["MIGRATE", "expected_revision", "explain", "store_revision"]

MIGRATE = "cd backend && ../gaia-env/bin/alembic upgrade head"


@lru_cache(maxsize=1)
def expected_revision() -> str | None:
    """The head of ``backend/alembic/versions``: the revision no other one builds on."""
    versions = ROOT / "backend" / "alembic" / "versions"
    revisions, downs = set(), set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?:: str)? = "([^"]+)"', text, re.M)
        down = re.search(r'^down_revision(?:: [^=]+)? = "([^"]+)"', text, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down:
            downs.add(down.group(1))
    heads = sorted(revisions - downs)
    return heads[-1] if heads else None


async def store_revision(conn) -> str | None:
    """The revision the store is at (``conn``: an asyncpg connection)."""
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    except Exception:  # noqa: BLE001 - no table: never migrated
        return None


def explain(current: str | None, expected: str | None) -> str | None:
    """What to do, or None when the store is current."""
    if expected is None or current == expected:
        return None
    at = f"at {current}" if current else "not migrated"
    return (f"the metadata store is {at}; this build needs {expected}: run `{MIGRATE}` "
            "(then run the command again)")


def check_sync(dsn: str, timeout: float = 5) -> str | None:
    """``explain`` for a command-line tool that failed (no event loop running)."""
    import asyncio

    import asyncpg

    async def run() -> str | None:
        conn = await asyncpg.connect(dsn=dsn, timeout=timeout)
        try:
            return explain(await store_revision(conn), expected_revision())
        finally:
            await conn.close()

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001 - the store itself is unreachable: the error says so
        return None
