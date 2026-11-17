"""The access audit log: append-only and hash-chained (``audit.auth_event``, D70).

Every row's hash is ``sha256(previous hash | canonical JSON of the row)``. Rows are
appended under a transaction-level advisory lock, so concurrent writers cannot fork the
chain; a row changed, removed or slipped in later breaks every hash after it, and
:func:`verify_chain` says where. The auditor's report prints the head hash: kept outside
Gaia (a ticket, an e-mail to the auditor), it also covers someone who could rewrite the
whole table.

``detail`` is checked: a key that names a password, secret or token is refused outright,
so a careless caller fails a test instead of writing a credential into the audit.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.locks import lock_key
from gaia.db.models.audit import AuthEvent

__all__ = ["GENESIS", "ChainCheck", "record", "verify_chain"]

GENESIS = "0" * 64
_SECRET_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|credential", re.IGNORECASE)
_LOCK = lock_key("auth-audit", uuid.UUID(int=0))


def _canonical(seq: int, at: datetime, actor: str | None, action: str, subject: str | None,
               detail: dict[str, Any], ip: str | None) -> str:
    return json.dumps({"seq": seq, "at": at.isoformat(), "actor": actor, "action": action,
                       "subject": subject, "detail": detail, "ip": ip},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(prev: str, body: str) -> str:
    return hashlib.sha256(f"{prev}|{body}".encode()).hexdigest()


def _check_detail(detail: dict[str, Any], path: str = "") -> None:
    for key, value in detail.items():
        if _SECRET_KEY.search(str(key)):
            raise ValueError(f"audit detail may not carry {path}{key!r}: it names a secret")
        if isinstance(value, dict):
            _check_detail(value, f"{path}{key}.")


async def record(session: AsyncSession, action: str, *, actor: str | None,
                 subject: str | None = None, detail: dict[str, Any] | None = None,
                 ip: str | None = None) -> AuthEvent:
    """Append one event (flushed, not committed: it commits with the change it records)."""
    detail = json.loads(json.dumps(detail or {}, default=str))
    _check_detail(detail)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})
    last = (await session.execute(
        select(AuthEvent.seq, AuthEvent.hash).order_by(AuthEvent.seq.desc()).limit(1)
    )).first()
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS
    at = datetime.now(UTC)
    body = _canonical(seq, at, actor, action, subject, detail, ip)
    row = AuthEvent(seq=seq, at=at, actor=actor, action=action, subject=subject,
                    detail=detail, ip=ip, prev_hash=prev, hash=_digest(prev, body))
    session.add(row)
    await session.flush()
    return row


@dataclass
class ChainCheck:
    ok: bool
    events: int
    head: str  # the last hash (GENESIS when empty)
    broken_at: int | None = None  # the first seq whose hash does not follow
    reason: str | None = None


async def verify_chain(session: AsyncSession) -> ChainCheck:
    rows = (await session.scalars(select(AuthEvent).order_by(AuthEvent.seq))).all()
    prev, expected_seq = GENESIS, 1
    for row in rows:
        if row.seq != expected_seq:
            return ChainCheck(False, len(rows), rows[-1].hash, row.seq,
                              f"sequence jumps from {expected_seq - 1} to {row.seq}")
        if row.prev_hash != prev:
            return ChainCheck(False, len(rows), rows[-1].hash, row.seq,
                              "does not point at the previous row")
        body = _canonical(row.seq, row.at, row.actor, row.action, row.subject, row.detail,
                          row.ip)
        if _digest(prev, body) != row.hash:
            return ChainCheck(False, len(rows), rows[-1].hash, row.seq,
                              "content changed after it was written")
        prev, expected_seq = row.hash, expected_seq + 1
    return ChainCheck(True, len(rows), prev)


async def count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AuthEvent)) or 0)
