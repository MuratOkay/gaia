"""The auditor's reports (Phase 5A, D70): the administrator's work, read-only.

Every endpoint needs the **auditor** role, which an administrator cannot hold (D68).
``?format=csv`` turns any list into a spreadsheet-ready file.

    GET /api/audit/summary            counts, the audit chain's state and head hash
    GET /api/audit/access             who may do what now, and through which directory role
    GET /api/audit/pii-processors     who sees personal data unmasked (KVKK, D69)
    GET /api/audit/grants             every grant ever made, with who granted and revoked it
    GET /api/audit/events             the access log (?action=&since=&limit=)
    GET /api/audit/chain              is the hash chain intact; where it breaks
    GET /api/audit/connection-changes data sources added, changed, probed, removed (4F)
    GET /api/audit/categories         each data category: its objects, who may read it (5C)
    GET /api/audit/refusals           questions refused by need-to-know, with what they reached for

The head hash in ``summary`` and ``chain`` is what makes the log tamper-evident beyond
the database: an auditor who writes it down (a ticket, an e-mail) can later prove that
nothing before it was rewritten.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select

from gaia.auth import audit
from gaia.auth.deps import get_auth, require
from gaia.auth.service import AuthService, Principal
from gaia.db.models.audit import AuthEvent, ConnectionChange

__all__ = ["router"]

router = APIRouter(prefix="/api/audit", tags=["audit"])
AUDITOR = require("auditor")


def _cell(value: Any) -> str:
    if isinstance(value, list | tuple | set):
        return "; ".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_cell(v)}" for k, v in value.items())
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else str(value)


def _out(rows: list[dict[str, Any]], fmt: str | None, name: str):
    if fmt != "csv":
        return rows
    buf = io.StringIO()
    columns = list(dict.fromkeys(k for r in rows for k in r))
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: _cell(r.get(k)) for k in columns})
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="gaia-{name}.csv"'})


@router.get("/summary")
async def summary(_who: Principal = Depends(AUDITOR),  # noqa: B008
                  auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    accounts = await auth.accounts()
    async with auth.factory() as s:
        check = await audit.verify_chain(s)
        local = await s.scalar(select(func.count()).select_from(AuthEvent).where(
            AuthEvent.action == "sign_in", AuthEvent.detail["local"].astext == "true"))
        refused = await s.scalar(select(func.count()).select_from(AuthEvent).where(
            AuthEvent.action.in_(("sign_in_refused", "forbidden"))))
        need_to_know = await s.scalar(select(func.count()).select_from(AuthEvent).where(
            AuthEvent.action == "access_refused"))
    return {"accounts": len(accounts),
            "by_role": {r: sum(1 for a in accounts if r in a["roles"])
                        for r in ("user", "sme", "admin", "auditor")},
            "pii_processors": sum(1 for a in accounts if a["pii_processor"]),
            "conflicts": [f"{a['login']}: {c}" for a in accounts for c in a["conflicts"]],
            "local_sign_ins": local, "refusals": refused,
            "questions_refused": need_to_know,
            "chain": {"ok": check.ok, "events": check.events, "head": check.head,
                      "broken_at": check.broken_at, "reason": check.reason}}


@router.get("/access")
async def access(format: str | None = None, _who: Principal = Depends(AUDITOR),  # noqa: A002, B008
                 auth: AuthService = Depends(get_auth)):  # noqa: B008
    return _out(await auth.accounts(), format, "access")


@router.get("/pii-processors")
async def pii_processors(format: str | None = None,  # noqa: A002
                         _who: Principal = Depends(AUDITOR),  # noqa: B008
                         auth: AuthService = Depends(get_auth)):  # noqa: B008
    rows = [{"login": a["login"], "directory": a["directory"], "display_name": a["display_name"],
             "enabled": a["enabled"], "through": a["via"].get("pii_processor", [])}
            for a in await auth.accounts() if a["pii_processor"]]
    return _out(rows, format, "pii-processors")


@router.get("/grants")
async def grants(format: str | None = None, _who: Principal = Depends(AUDITOR),  # noqa: A002, B008
                 auth: AuthService = Depends(get_auth)):  # noqa: B008
    return _out(await auth.grants(include_revoked=True), format, "grants")


@router.get("/events")
async def events(action: str | None = None, since: datetime | None = None, limit: int = 500,
                 format: str | None = None, _who: Principal = Depends(AUDITOR),  # noqa: A002, B008
                 auth: AuthService = Depends(get_auth)):  # noqa: B008
    async with auth.factory() as s:
        q = select(AuthEvent).order_by(AuthEvent.seq.desc()).limit(max(1, min(limit, 10_000)))
        if action:
            q = q.where(AuthEvent.action == action)
        if since:
            q = q.where(AuthEvent.at >= since)
        rows = [{"seq": e.seq, "at": e.at, "actor": e.actor, "action": e.action,
                 "subject": e.subject, "detail": e.detail, "ip": e.ip, "hash": e.hash}
                for e in (await s.scalars(q)).all()]
    return _out(rows, format, "events")


@router.get("/chain")
async def chain(_who: Principal = Depends(AUDITOR),  # noqa: B008
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        check = await audit.verify_chain(s)
    return {"ok": check.ok, "events": check.events, "head": check.head,
            "broken_at": check.broken_at, "reason": check.reason}


@router.get("/categories")
async def categories(format: str | None = None,  # noqa: A002
                     _who: Principal = Depends(AUDITOR),  # noqa: B008
                     auth: AuthService = Depends(get_auth)):  # noqa: B008
    from gaia.access.categories import CategoryService

    rows = await CategoryService(auth.factory).report(await auth.accounts())
    return _out(rows, format, "categories")


@router.get("/refusals")
async def refusals(format: str | None = None, limit: int = 500,  # noqa: A002
                   _who: Principal = Depends(AUDITOR),  # noqa: B008
                   auth: AuthService = Depends(get_auth)):  # noqa: B008
    async with auth.factory() as s:
        rows = [{"at": e.at, "actor": e.actor, "subject": e.subject,
                 "code": (e.detail or {}).get("code"),
                 "reached_for": (e.detail or {}).get("reached_for") or []}
                for e in (await s.scalars(select(AuthEvent).where(
                    AuthEvent.action == "access_refused").order_by(AuthEvent.seq.desc())
                    .limit(max(1, min(limit, 10_000))))).all()]
    return _out(rows, format, "refusals")


@router.get("/connection-changes")
async def connection_changes(format: str | None = None,  # noqa: A002
                             _who: Principal = Depends(AUDITOR),  # noqa: B008
                             auth: AuthService = Depends(get_auth)):  # noqa: B008
    async with auth.factory() as s:
        rows = [{"at": c.created_at, "data_source": c.data_source_name, "action": c.action,
                 "actor": c.actor, "detail": c.detail}
                for c in (await s.scalars(select(ConnectionChange).order_by(
                    ConnectionChange.created_at.desc()).limit(5000))).all()]
    return _out(rows, format, "connection-changes")
