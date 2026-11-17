"""Operational tools (target checks, embedding checks).

Importing this package installs one exception hook (D76): a tool that dies on a table or
column the metadata store does not have says, in one line, which migration is missing
instead of only printing a long traceback.
"""

from __future__ import annotations

import sys


def _hook(exc_type, exc, tb) -> None:
    text = f"{exc_type.__name__}: {exc}"
    missing = ("does not exist" in text and ("column" in text or "relation" in text)
               and ("ProgrammingError" in text or "Undefined" in text))
    if missing:
        try:
            from gaia.db.engine import asyncpg_dsn
            from gaia.db.revision import check_sync

            advice = check_sync(asyncpg_dsn())
        except Exception:  # noqa: BLE001 - the hook must never hide the original error
            advice = None
        if advice:
            first = str(exc).splitlines()[0][:200]
            print(f"FAILED: {first}\n  {advice}", file=sys.stderr)
            return
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _hook
