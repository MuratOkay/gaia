"""The SQL behind the answers, and the runs behind the meanings (Phase 5D, 5E).

**Queries** (P5-7, D70): the SQL of an answer is never shown to the ``user`` role (P4-2);
SMEs see the queries asked of the data sources they review, administrators and auditors all
of them. Each entry is an attempt of ``chat.query_run``: the question, who asked, what the
guard decided, what ran, how long it took; a repair chain reads as attempts 1, 2, 3.

    GET /api/queries?data_source=&asked_by=&status=&limit=&offset=
    GET /api/queries/{id}                 one query and the other attempts of its question

**Runs** (5E, read-only; starting, stopping and scheduling runs are Phase 7): reasoning
and profiling runs per data source, with what they did and what the model calls cost.

    GET /api/runs?data_source=&limit=     administrators and auditors
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select

from gaia.auth.deps import get_auth, principal
from gaia.auth.service import AuthService, Principal
from gaia.db.models.catalog import DataSource
from gaia.db.models.chat import QueryRun

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["queries"])


async def _refuse(request: Request, who: Principal, auth: AuthService, needed: str) -> None:
    from gaia.auth.deps import _refuse as refuse

    await refuse(auth, request, who, needed)


def _scope(who: Principal) -> set[str] | None:
    """The data source ids whose queries this person sees; None: all of them."""
    if who.has("admin", "auditor"):
        return None
    if who.has("sme"):
        return set(who.effective.sme_sources)
    return set()


def _view(r: QueryRun, ds: str) -> dict[str, Any]:
    guard = r.guard or {}
    return {"id": str(r.id), "data_source": ds, "question": r.question, "asked_by": r.asked_by,
            "origin": r.origin, "attempt": r.attempt,
            "parent_id": str(r.parent_id) if r.parent_id else None, "status": r.status,
            "created_at": r.created_at, "dialect": r.dialect,
            "submitted_sql": r.submitted_sql, "executed_sql": r.executed_sql,
            "errors": guard.get("errors") or [], "warnings": guard.get("warnings") or [],
            "tables": guard.get("tables") or [], "joins": guard.get("joins") or [],
            "forbidden": guard.get("forbidden") or [], "row_count": r.row_count,
            "row_cap_reached": r.row_cap_reached, "elapsed_ms": r.elapsed_ms,
            "error": r.error, "model": r.model, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens}


@router.get("/queries")
async def queries(request: Request, data_source: str | None = None,
                  asked_by: str | None = None, status: str | None = None,
                  limit: int = 100, offset: int = 0,
                  who: Principal = Depends(principal),  # noqa: B008
                  auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    scope = _scope(who)
    if scope is not None and not scope:
        await _refuse(request, who, auth, "sme, admin or auditor")
    async with auth.factory() as s:
        q = select(QueryRun, DataSource.name).join(
            DataSource, DataSource.id == QueryRun.data_source_id)
        if scope is not None:
            q = q.where(QueryRun.data_source_id.in_([uuid.UUID(i) for i in scope]))
        if data_source:
            q = q.where(DataSource.name == data_source)
        if asked_by:
            q = q.where(QueryRun.asked_by == asked_by.lower())
        if status == "refused":
            q = q.where(QueryRun.guard["errors"].astext.contains('"forbidden"')
                        | QueryRun.guard["errors"].astext.contains('"personal_data"'))
        elif status:
            q = q.where(QueryRun.status == status)
        total = await s.scalar(select(func.count()).select_from(q.subquery()))
        rows = (await s.execute(q.order_by(QueryRun.created_at.desc(), QueryRun.attempt.desc())
                                .limit(max(1, min(limit, 500))).offset(max(0, offset)))).all()
        return {"total": total, "items": [_view(r, ds) for r, ds in rows]}


@router.get("/queries/{query_id}")
async def query(query_id: uuid.UUID, request: Request,
                who: Principal = Depends(principal),  # noqa: B008
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    scope = _scope(who)
    if scope is not None and not scope:
        await _refuse(request, who, auth, "sme, admin or auditor")
    async with auth.factory() as s:
        r = await s.get(QueryRun, query_id)
        if r is None or (scope is not None and str(r.data_source_id) not in scope):
            raise HTTPException(404, "no such query")  # another's data source: not there
        ds = await s.get(DataSource, r.data_source_id)
        root = r
        for _ in range(10):
            if root.parent_id is None:
                break
            parent = await s.get(QueryRun, root.parent_id)
            if parent is None:
                break
            root = parent
        chain = [root]
        for _ in range(10):
            child = await s.scalar(select(QueryRun).where(QueryRun.parent_id == chain[-1].id))
            if child is None:
                break
            chain.append(child)
        return _view(r, ds.name) | {"attempts": [_view(x, ds.name) for x in chain]}


@router.get("/runs")
async def runs(request: Request, data_source: str | None = None, limit: int = 50,
               who: Principal = Depends(principal),  # noqa: B008
               auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    from gaia.db.models.audit import LLMCall
    from gaia.db.models.profiling import ProfileRun
    from gaia.db.models.semantics import ReasoningRun

    if not who.has("admin", "auditor"):
        await _refuse(request, who, auth, "admin or auditor")
    limit = max(1, min(limit, 500))
    async with auth.factory() as s:
        sources = {d.id: d.name for d in (await s.scalars(select(DataSource))).all()}
        wanted = [i for i, n in sources.items() if not data_source or n == data_source]
        reasoning = (await s.scalars(select(ReasoningRun).where(
            ReasoningRun.data_source_id.in_(wanted)).order_by(ReasoningRun.started_at.desc())
            .limit(limit))).all()
        profiling = (await s.scalars(select(ProfileRun).where(
            ProfileRun.data_source_id.in_(wanted)).order_by(ProfileRun.started_at.desc())
            .limit(limit))).all()
        cost = {r[0]: r[1:] for r in (await s.execute(
            select(LLMCall.run_id, func.count(), func.sum(LLMCall.prompt_tokens),
                   func.sum(LLMCall.completion_tokens), func.sum(LLMCall.cost))
            .where(LLMCall.run_id.in_([r.id for r in reasoning] or [uuid.UUID(int=0)]))
            .group_by(LLMCall.run_id))).all()}
        totals = []
        for ds_id in wanted:
            row = (await s.execute(select(
                func.count(), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
                func.coalesce(func.sum(LLMCall.completion_tokens), 0), func.sum(LLMCall.cost))
                .where(LLMCall.data_source_id == ds_id))).one()
            asked = await s.scalar(select(func.count()).select_from(QueryRun).where(
                QueryRun.data_source_id == ds_id, or_(QueryRun.parent_id.is_(None),
                                                      QueryRun.attempt == 1)))
            totals.append({"data_source": sources[ds_id], "llm_calls": row[0],
                           "prompt_tokens": int(row[1]), "completion_tokens": int(row[2]),
                           "cost": float(row[3]) if row[3] is not None else None,
                           "queries": asked})
    return {
        "totals": totals,
        "reasoning": [{
            "id": str(r.id), "data_source": sources.get(r.data_source_id), "kind": "reasoning",
            "status": r.status, "started_at": r.started_at, "finished_at": r.finished_at,
            "model": r.model, "tables_in_scope": r.tables_in_scope,
            "tables_reasoned": r.tables_reasoned, "tables_skipped": r.tables_skipped,
            "tables_failed": r.tables_failed, "llm_calls": r.llm_calls,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "cost": float(cost[r.id][3]) if r.id in cost and cost[r.id][3] is not None
            else None, "wall_ms": r.wall_ms, "notes": list(r.notes or [])[:20]}
            for r in reasoning],
        "profiling": [{
            "id": str(r.id), "data_source": sources.get(r.data_source_id), "kind": "profiling",
            "stage": r.stage, "status": r.status, "started_at": r.started_at,
            "finished_at": r.finished_at, "tables_in_scope": r.tables_in_scope,
            "tables_profiled": r.tables_profiled, "tables_reused": r.tables_reused,
            "tables_skipped": r.tables_skipped, "tables_failed": r.tables_failed,
            "queries": r.queries, "wall_ms": r.wall_ms, "notes": list(r.notes or [])[:20]}
            for r in profiling],
    }
