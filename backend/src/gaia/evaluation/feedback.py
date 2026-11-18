"""People's "Was this right?" under Gaia's answers, and the SME's decision (Phase 6D, D87;
P6-4).

* Anyone who chats may say of an answer in their own conversation: **right**, **partly** or
  **wrong**, with a note. They may change it; a "right" is counted, never reviewed.
* A "partly" or "wrong" opens an item for the **SMEs of the data source** (*Review →
  Answers*): the question, what Gaia understood and assumed, the SQL, the result as stored (the
  personal data in it masked), the confidence shown, and the person's note. The SME decides
  what the answer really was — right, partly, wrong — and, when it was not right, why: a
  **meaning** (a column read wrongly: fix it in the queue), a missing **definition** (the
  glossary), a **join**, the **model**'s reading, the **data** itself, or the **question**
  (unclear, or outside the data). Only a decided item is a label (answer calibration, 6D): a
  person's doubt alone is not a verdict — they may be wrong, and people report what surprises
  them.
* Withdrawn before a decision (changed to "right"), the item closes; changed after one, the
  decision stands — it was about the answer, which has not changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import DataSource
from gaia.db.models.chat import (
    FEEDBACK_CAUSES,
    FEEDBACK_VERDICTS,
    Conversation,
    Feedback,
    Message,
    QueryRun,
)

__all__ = ["FeedbackError", "FeedbackNotFound", "all_feedback", "decide", "feedback_dict",
           "give", "open_counts", "review_items", "reviewers"]

NOTE_MAX = 2000
KEEP = object()  # D94: a verdict sent without a note keeps the note given before


class FeedbackError(ValueError):
    """A request that cannot be done (400)."""


class FeedbackNotFound(LookupError):
    """No such answer or item for this person (404)."""


def feedback_dict(f: Feedback | None) -> dict[str, Any] | None:
    """What the person who gave it sees."""
    if f is None:
        return None
    return {"verdict": f.verdict, "note": f.note, "status": f.status, "decision": f.decision,
            "cause": f.cause, "decision_note": f.decision_note, "decided_at": f.decided_at,
            "updated_at": f.updated_at}


async def give(session: AsyncSession, message_id: uuid.UUID, *, owner: str, login: str,
               verdict: str, note: Any = None) -> Feedback:
    """The person's verdict on an answer of their own conversation (created or changed).
    ``note=KEEP``: the verdict alone changes and the note given before stays (D94)."""
    if verdict not in FEEDBACK_VERDICTS:
        raise FeedbackError(f"verdict is one of {', '.join(FEEDBACK_VERDICTS)}")
    row = (await session.execute(
        select(Message, Conversation).join(Conversation,
                                           Conversation.id == Message.conversation_id)
        .where(Message.id == message_id))).first()
    if row is None or row[1].created_by != owner:
        raise FeedbackNotFound("no such answer in your conversations")
    message, conv = row
    if message.role != "assistant" or message.kind != "answer":
        raise FeedbackError("only an answer can be right or wrong")
    keep = note is KEEP
    note = None if keep else ((note or "").strip()[:NOTE_MAX] or None)
    now = datetime.now(UTC)
    f = await session.scalar(select(Feedback).where(Feedback.message_id == message_id,
                                                    Feedback.given_by == login))
    if f is None:
        f = Feedback(message_id=message_id, data_source_id=conv.data_source_id, given_by=login,
                     verdict=verdict, note=note, created_at=now, updated_at=now,
                     status="none" if verdict == "right" else "open")
        session.add(f)
    else:
        f.verdict, f.updated_at = verdict, now
        if not keep:
            f.note = note
        if f.status != "decided":
            f.status = "none" if verdict == "right" else "open"
    await session.flush()
    return f


async def reviewers(session: AsyncSession, data_source_id: uuid.UUID) -> list[str]:
    """Who may decide the doubted answers of a data source (D94): its current SME grants —
    ``acme/akaya`` for an account, ``acme/GAIA-X (role)`` for a directory role. Empty: nobody
    will ever see them (an administrator's SME grant is not applied, D68, and is left out)."""
    from gaia.db.models.auth import Account, Directory, DirectoryRole, Grant

    grants = (await session.scalars(select(Grant).where(
        Grant.privilege == "sme", Grant.data_source_id == data_source_id,
        Grant.revoked_at.is_(None)))).all()
    out: list[str] = []
    for g in grants:
        if g.account_id is not None:
            row = (await session.execute(
                select(Account.login, Directory.name).outerjoin(
                    Directory, Directory.id == Account.directory_id)
                .where(Account.id == g.account_id, Account.enabled.is_(True)))).first()
            if row is not None:
                admin = await session.scalar(select(func.count()).select_from(Grant).where(
                    Grant.account_id == g.account_id, Grant.privilege == "admin",
                    Grant.revoked_at.is_(None)))
                if not admin:
                    out.append(f"{row[1]}/{row[0]}" if row[1] else row[0])
        elif g.directory_role_id is not None:
            row = (await session.execute(
                select(DirectoryRole.name, Directory.name).join(
                    Directory, Directory.id == DirectoryRole.directory_id)
                .where(DirectoryRole.id == g.directory_role_id))).first()
            if row is not None:
                out.append(f"{row[1]}/{row[0]} (role)")
    return sorted(set(out))


async def all_feedback(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    """Every "Was this right?", newest first, with its data source and who reviews it — the
    operator's view across data sources (``make feedback``, D94)."""
    rows = (await session.execute(
        select(Feedback, Message, DataSource.name, DataSource.id)
        .join(Message, Message.id == Feedback.message_id)
        .join(DataSource, DataSource.id == Feedback.data_source_id)
        .order_by(Feedback.updated_at.desc()).limit(limit))).all()
    who: dict[uuid.UUID, list[str]] = {}
    out = []
    for f, m, ds_name, ds_id in rows:
        if ds_id not in who:
            who[ds_id] = await reviewers(session, ds_id)
        out.append({"updated_at": f.updated_at, "data_source": ds_name, "given_by": f.given_by,
                    "verdict": f.verdict, "note": f.note, "status": f.status,
                    "decision": f.decision, "decided_by": f.decided_by,
                    "question": (m.payload or {}).get("question"), "reviewers": who[ds_id]})
    return out


async def open_counts(session: AsyncSession, data_source_ids: list[uuid.UUID]
                      ) -> dict[str, int]:
    """Open items per data source id (the SME's badge)."""
    if not data_source_ids:
        return {}
    rows = (await session.execute(
        select(Feedback.data_source_id, func.count()).where(
            Feedback.data_source_id.in_(data_source_ids), Feedback.status == "open")
        .group_by(Feedback.data_source_id))).all()
    return {str(ds): n for ds, n in rows}


def _item(f: Feedback, m: Message, run: QueryRun | None, *, sql: bool) -> dict[str, Any]:
    p = m.payload or {}
    return {
        "id": str(f.id), "message_id": str(m.id), "status": f.status,
        "verdict": f.verdict, "note": f.note, "given_by": f.given_by,
        "created_at": f.created_at, "updated_at": f.updated_at,
        "question": p.get("question"), "narrative": m.content,
        "interpretation": p.get("interpretation"), "assumptions": p.get("assumptions") or [],
        "uncertain": p.get("uncertain") or [], "confidence": p.get("confidence"),
        "columns": p.get("columns") or [], "rows": (p.get("rows") or [])[:50],
        "row_count": p.get("row_count"), "masked_columns": p.get("masked_columns") or [],
        "sql": (run.executed_sql or run.submitted_sql) if (sql and run is not None) else None,
        "model": run.model if run is not None else None,
        "answered_at": m.created_at,
        "decision": f.decision, "cause": f.cause, "decision_note": f.decision_note,
        "decided_by": f.decided_by, "decided_at": f.decided_at,
    }


async def review_items(session: AsyncSession, data_source: str, *, status: str = "open",
                       limit: int = 50, offset: int = 0, sql: bool = True) -> dict[str, Any]:
    """The answers people doubted on a data source: ``open`` (to decide) or ``decided``."""
    if status not in ("open", "decided"):
        raise FeedbackError("status is open or decided")
    ds = await session.scalar(select(DataSource).where(DataSource.name == data_source))
    if ds is None:
        raise FeedbackNotFound(f"no data source {data_source!r}")
    base = (select(Feedback, Message, QueryRun)
            .join(Message, Message.id == Feedback.message_id)
            .outerjoin(QueryRun, QueryRun.id == Message.query_run_id)
            .where(Feedback.data_source_id == ds.id, Feedback.status == status))
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    order = Feedback.created_at.asc() if status == "open" else Feedback.decided_at.desc()
    rows = (await session.execute(base.order_by(order).limit(limit).offset(offset))).all()
    return {"total": total, "items": [_item(f, m, r, sql=sql) for f, m, r in rows]}


async def decide(session: AsyncSession, data_source: str, feedback_id: uuid.UUID, *,
                 reviewer: str, decision: str, cause: str | None = None,
                 note: str | None = None) -> dict[str, Any]:
    """The SME's verdict on a doubted answer — the label — and, if not right, why."""
    if decision not in FEEDBACK_VERDICTS:
        raise FeedbackError(f"decision is one of {', '.join(FEEDBACK_VERDICTS)}")
    if decision != "right" and cause not in FEEDBACK_CAUSES:
        raise FeedbackError(f"say why it was not right: {', '.join(FEEDBACK_CAUSES)}")
    row = (await session.execute(
        select(Feedback, Message, QueryRun, DataSource.name)
        .join(Message, Message.id == Feedback.message_id)
        .outerjoin(QueryRun, QueryRun.id == Message.query_run_id)
        .join(DataSource, DataSource.id == Feedback.data_source_id)
        .where(Feedback.id == feedback_id))).first()
    if row is None or row[3] != data_source:
        raise FeedbackNotFound("no such item on this data source")
    f, m, run, _ds = row
    if f.status == "none":
        raise FeedbackError("the person said it was right: there is nothing to decide")
    now = datetime.now(UTC)
    f.status, f.decision = "decided", decision
    f.cause = cause if decision != "right" else None
    f.decision_note = (note or "").strip()[:NOTE_MAX] or None
    f.decided_by, f.decided_at, f.updated_at = reviewer, now, now
    await session.flush()
    return _item(f, m, run, sql=True)
