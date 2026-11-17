"""One run of a kind per data source at a time (D33, extended to reasoning in 5.0).

A session-level PostgreSQL advisory lock, taken with ``pg_try_advisory_lock`` on a
dedicated metadata connection: a second run is refused at once instead of waiting,
and a crashed run releases the lock when its connection closes. The key is derived
from the kind and the data source, so different kinds (``profiling``, ``reasoning``)
do not exclude each other — the same as before this module existed for profiling.

Why reasoning needs it (2026-11-17 triage, T2): two overlapping ``make reason`` runs
plan from the same state, both call the model for every table, and whichever writes
last wins. Nothing is corrupt, but the cost doubles; an admin screen that can start
runs must not allow it.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["DataSourceBusy", "DataSourceLock", "lock_key"]


class DataSourceBusy(RuntimeError):
    """Another run of the same kind holds the lock for this data source."""


def lock_key(kind: str, data_source_id: uuid.UUID) -> int:
    """A signed 64-bit advisory-lock key. ``gaia-profiling:<id>`` is the key profiling has
    always used, so a host upgraded mid-run still sees the old lock."""
    digest = hashlib.sha256(f"gaia-{kind}:{data_source_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class DataSourceLock:
    """``async with DataSourceLock(session, ds_id, "reasoning"):`` — raises
    :class:`DataSourceBusy` (or ``error``) when another run holds it."""

    def __init__(
        self,
        session: AsyncSession,
        data_source_id: uuid.UUID,
        kind: str,
        *,
        error: type[Exception] = DataSourceBusy,
        message: str | None = None,
    ) -> None:
        self.engine = session.bind
        self.key = lock_key(kind, data_source_id)
        self.error = error
        self.message = message or f"another {kind} run holds the lock for this data source"
        self.conn = None

    async def __aenter__(self) -> DataSourceLock:
        self.conn = await self.engine.connect()
        got = await self.conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})
        await self.conn.commit()
        if not got:
            await self.conn.close()
            self.conn = None
            raise self.error(self.message)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.conn is None:
            return
        try:
            await self.conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            await self.conn.commit()
        finally:
            await self.conn.close()
            self.conn = None
