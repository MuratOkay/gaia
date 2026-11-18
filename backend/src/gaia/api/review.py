"""SME review over HTTP (Phase 5B; D16, D69, D73).

Every endpoint needs a signed-in **SME of that data source** (an ``sme`` grant for it,
D68); an administrator never is one. Decisions are signed with the SME's login.

    GET  /api/review                                   the data sources I review, queue sizes
    GET  /api/review/{ds}/queue?status=open|done|stale&reason=&table=&limit=&offset=
    GET  /api/review/{ds}/columns/{column_id}          everything Gaia knows about the column
    POST /api/review/{ds}/columns/{column_id}/decision
         {"action": "accept"|"correct"|"reject"|"unused", "note"?, "changes"?, "value_meanings"?}
    GET  /api/review/{ds}/tables                       every table, with what waits in each
    GET  /api/review/{ds}/tables/{table_id}            the table's meaning and its columns
    POST /api/review/{ds}/tables/{table_id}/decision   {"action": "accept"|"correct", …}
    POST /api/review/{ds}/tables/{table_id}/category   {"category", "note"?}: what the data is (5C)
    POST /api/review/{ds}/join-hints/{hint_id}/decision  {"action": "confirm"|"reject", "note"?}
    GET  /api/review/{ds}/glossary
    POST /api/review/{ds}/glossary                     {"term", "definition", "synonyms"?}
    PATCH /api/review/{ds}/glossary/{term_id}          {"definition"?, "synonyms"?}
    DELETE /api/review/{ds}/glossary/{term_id}         withdrawn (kept, audited)
    GET  /api/review/{ds}/feedback?status=open|decided answers people doubted (6D, D87)
    POST /api/review/{ds}/feedback/{id}/decision       {"decision": right|partly|wrong,
                                                        "cause"?, "note"?}
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from gaia.auth import audit
from gaia.auth.deps import client_ip, get_auth, principal
from gaia.auth.service import AuthService, Principal
from gaia.db.models.catalog import DataSource
from gaia.semantics.sme import Reviewer, ReviewError, ReviewNotFound, ReviewService

__all__ = ["router"]

router = APIRouter(prefix="/api/review", tags=["review"])


class ColumnDecision(BaseModel):
    action: str
    note: str | None = None
    changes: dict[str, Any] | None = None
    value_meanings: dict[str, str] | None = None


class TableDecision(BaseModel):
    action: str
    note: str | None = None
    changes: dict[str, Any] | None = None


class CategoryDecision(BaseModel):
    category: str = Field(min_length=1)
    note: str | None = None


class HintDecision(BaseModel):
    action: str
    note: str | None = None


class FeedbackDecision(BaseModel):
    decision: str
    cause: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class TermChange(BaseModel):
    definition: str | None = None
    synonyms: list[str] | None = None


class Term(BaseModel):
    term: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)


def get_review(request: Request) -> ReviewService:
    service = getattr(request.app.state, "review", None)
    if service is None:
        auth: AuthService = get_auth(request)
        service = request.app.state.review = ReviewService(auth.settings, auth.factory)
    return service


async def _sme(request: Request, who: Principal = Depends(principal),  # noqa: B008
               auth: AuthService = Depends(get_auth)) -> Principal:  # noqa: B008
    if not who.has("sme"):
        async with auth.factory() as s:
            await audit.record(s, "forbidden", actor=who.login,
                               subject=f"{request.method} {request.url.path}",
                               detail={"needed": "sme", "roles": sorted(who.effective.roles)},
                               ip=client_ip(request))
            await s.commit()
        raise HTTPException(403, "this needs the sme role")
    return who


async def reviewer_of(data_source: str, request: Request,
                      who: Principal = Depends(_sme),  # noqa: B008
                      auth: AuthService = Depends(get_auth)) -> Reviewer:  # noqa: B008
    """The SME, if they review this data source (404 for one that does not exist, 403 for
    one they do not review — and that refusal is audited)."""
    async with auth.factory() as s:
        ds = await s.scalar(select(DataSource).where(DataSource.name == data_source))
        if ds is None:
            raise HTTPException(404, f"no data source {data_source!r}")
        if not who.effective.reviews(str(ds.id)):
            await audit.record(s, "forbidden", actor=who.login,
                               subject=f"{request.method} {request.url.path}",
                               detail={"needed": f"sme of {data_source}"},
                               ip=client_ip(request))
            await s.commit()
            raise HTTPException(403, f"you do not review {data_source}")
    return Reviewer(who.login, pii_processor=who.effective.pii_processor)


async def _run(work):
    try:
        return await work
    except ReviewNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ReviewError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("")
async def sources(who: Principal = Depends(_sme),  # noqa: B008
                  review: ReviewService = Depends(get_review)) -> list[dict[str, Any]]:  # noqa: B008
    found = await review.sources(who.effective.sme_sources)
    from gaia.evaluation.feedback import open_counts

    async with review.factory() as s:  # answers people doubted, waiting (6D)
        counts = await open_counts(s, [uuid.UUID(d["id"]) for d in found])
    return [d | {"feedback_open": counts.get(d["id"], 0)} for d in found]


@router.get("/{data_source}/feedback")
async def feedback(data_source: str, status: str = "open", limit: int = 50, offset: int = 0,
                   _r: Reviewer = Depends(reviewer_of),  # noqa: B008
                   review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    """The answers people said were partly or wrong, for the SME to decide (6D)."""
    from gaia.evaluation.feedback import FeedbackError, FeedbackNotFound, review_items

    async with review.factory() as s:
        try:
            return await review_items(s, data_source, status=status,
                                      limit=max(1, min(limit, 200)), offset=max(0, offset))
        except FeedbackNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except FeedbackError as exc:
            raise HTTPException(400, str(exc)) from None


@router.post("/{data_source}/feedback/{feedback_id}/decision")
async def decide_feedback(data_source: str, feedback_id: uuid.UUID, body: FeedbackDecision,
                          request: Request, r: Reviewer = Depends(reviewer_of),  # noqa: B008
                          review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    """What the doubted answer really was — the label — and why, if it was not right."""
    from gaia.evaluation.feedback import FeedbackError, FeedbackNotFound, decide

    async with review.factory() as s:
        try:
            item = await decide(s, data_source, feedback_id, reviewer=r.login,
                                decision=body.decision, cause=body.cause, note=body.note)
        except FeedbackNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except FeedbackError as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "feedback_decided", actor=r.login,
                           subject=f"{data_source}: {str(item.get('question') or '')[:200]}",
                           detail={"feedback": str(feedback_id), "decision": body.decision,
                                   "cause": item.get("cause")}, ip=client_ip(request))
        await s.commit()
        return item


@router.get("/{data_source}/queue")
async def queue(data_source: str, status: str = "open", reason: str | None = None,
                table: str | None = None, limit: int = 50, offset: int = 0,
                _r: Reviewer = Depends(reviewer_of),  # noqa: B008
                review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    if status not in ("open", "done", "stale"):
        raise HTTPException(400, "status is open, done or stale")
    return await _run(review.queue(data_source, status=status, reason=reason, table=table,
                                   limit=max(1, min(limit, 500)), offset=max(0, offset)))


@router.get("/{data_source}/columns/{column_id}")
async def column(data_source: str, column_id: uuid.UUID,
                 r: Reviewer = Depends(reviewer_of),  # noqa: B008
                 review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.column(data_source, column_id, r))


@router.post("/{data_source}/columns/{column_id}/decision")
async def decide_column(data_source: str, column_id: uuid.UUID, body: ColumnDecision,
                        r: Reviewer = Depends(reviewer_of),  # noqa: B008
                        review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.decide_column(data_source, column_id, body.action, r,
                                           note=body.note, changes=body.changes,
                                           value_meanings=body.value_meanings))


@router.get("/{data_source}/tables")
async def tables(data_source: str, _r: Reviewer = Depends(reviewer_of),  # noqa: B008
                 review: ReviewService = Depends(get_review)) -> list[dict[str, Any]]:  # noqa: B008
    return await _run(review.tables(data_source))


@router.get("/{data_source}/tables/{table_id}")
async def table(data_source: str, table_id: uuid.UUID,
                _r: Reviewer = Depends(reviewer_of),  # noqa: B008
                review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.table(data_source, table_id))


@router.post("/{data_source}/tables/{table_id}/decision")
async def decide_table(data_source: str, table_id: uuid.UUID, body: TableDecision,
                       r: Reviewer = Depends(reviewer_of),  # noqa: B008
                       review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.decide_table(data_source, table_id, body.action, r,
                                          note=body.note, changes=body.changes))


@router.post("/{data_source}/tables/{table_id}/category")
async def decide_category(data_source: str, table_id: uuid.UUID, body: CategoryDecision,
                          r: Reviewer = Depends(reviewer_of),  # noqa: B008
                          auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    """The SME states what the object's data is (5C): its category, confirmed. Who may see
    that category stays the administrator's decision (D70)."""
    from gaia.access.categories import CategoryError, CategoryService

    try:
        return await CategoryService(auth.factory).assign(
            data_source, table_id, body.category, actor=r.login, source="sme", note=body.note)
    except CategoryError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/{data_source}/join-hints/{hint_id}/decision")
async def decide_hint(data_source: str, hint_id: uuid.UUID, body: HintDecision,
                      r: Reviewer = Depends(reviewer_of),  # noqa: B008
                      review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.decide_hint(data_source, hint_id, body.action, r, note=body.note))


@router.get("/{data_source}/glossary")
async def glossary(data_source: str, _r: Reviewer = Depends(reviewer_of),  # noqa: B008
                   review: ReviewService = Depends(get_review)) -> list[dict[str, Any]]:  # noqa: B008
    return await _run(review.glossary(data_source))


@router.post("/{data_source}/glossary", status_code=201)
async def add_term(data_source: str, body: Term, r: Reviewer = Depends(reviewer_of),  # noqa: B008
                   review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.add_term(data_source, body.term, body.definition, r,
                                      synonyms=body.synonyms))


@router.patch("/{data_source}/glossary/{term_id}")
async def change_term(data_source: str, term_id: uuid.UUID, body: TermChange,
                      r: Reviewer = Depends(reviewer_of),  # noqa: B008
                      review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.change_term(data_source, term_id, r, definition=body.definition,
                                         synonyms=body.synonyms))


@router.delete("/{data_source}/glossary/{term_id}")
async def remove_term(data_source: str, term_id: uuid.UUID,
                      r: Reviewer = Depends(reviewer_of),  # noqa: B008
                      review: ReviewService = Depends(get_review)) -> dict[str, Any]:  # noqa: B008
    return await _run(review.change_term(data_source, term_id, r, remove=True))
