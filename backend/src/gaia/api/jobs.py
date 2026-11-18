"""Jobs, schedules and alerts over HTTP (Phase 7C; P5-12; D98).

Administrators start, cancel and schedule; auditors read (as *Runs* since 5E). Every change
is audited with the administrator's login.

    GET    /api/jobs?data_source=…&limit=…        admin, auditor
    GET    /api/jobs/schedules                     admin, auditor
    PUT    /api/jobs/schedules                     admin   {"data_source", "kind", "when",
                                                            "params"?}
    POST   /api/jobs/schedules/{id}/enabled        admin   {"enabled": bool}
    GET    /api/jobs/alerts                        admin
    POST   /api/jobs/alerts/{id}/ack               admin
    GET    /api/jobs/{id}                          admin, auditor (with its log)
    POST   /api/jobs                               admin   {"kind", "data_source", "params"?}
    POST   /api/jobs/{id}/cancel                   admin
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from gaia.auth import audit
from gaia.auth.deps import client_ip, get_auth, require
from gaia.auth.service import AuthService, Principal
from gaia.db.models.ops import Alert, Job, JobEvent, Schedule
from gaia.jobs.queue import (
    JobNotFound,
    JobRefused,
    acknowledge,
    enqueue,
    job_view,
    open_alerts,
    request_cancel,
)
from gaia.jobs.schedules import add_schedule, schedule_view
from gaia.jobs.worker import known_data_sources

__all__ = ["router"]

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
ADMIN = require("admin")
READ = require("admin", "auditor")

# what a person may set on a job (the rest is the configuration's)
PARAMS = {"budget", "ignore_window", "no_claim_tests", "runs", "file", "no_vectors"}


class NewJob(BaseModel):
    kind: str
    data_source: str
    params: dict[str, Any] = Field(default_factory=dict)


class NewSchedule(BaseModel):
    data_source: str
    kind: str
    when: str
    params: dict[str, Any] = Field(default_factory=dict)


class Enabled(BaseModel):
    enabled: bool


def _params(params: dict[str, Any]) -> dict[str, Any]:
    unknown = set(params) - PARAMS
    if unknown:
        raise HTTPException(400, f"unknown parameter(s): {', '.join(sorted(unknown))} "
                                 f"({', '.join(sorted(PARAMS))})")
    return params


def _alert_view(a: Alert) -> dict[str, Any]:
    return {"id": str(a.id), "kind": a.kind, "data_source": a.data_source,
            "job_id": str(a.job_id) if a.job_id else None, "message": a.message,
            "detail": a.detail, "created_at": a.created_at,
            "acknowledged_at": a.acknowledged_at, "acknowledged_by": a.acknowledged_by}


@router.get("")
async def jobs(data_source: str | None = None, limit: int = 50,
               _who: Principal = Depends(READ),  # noqa: B008
               auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500)))
        if data_source:
            q = q.where(Job.data_source == data_source)
        rows = (await s.scalars(q)).all()
        known = sorted(await known_data_sources(s, auth.settings))
    return {"jobs": [job_view(j) for j in rows], "data_sources": known,
            "kinds": ["refresh", "drift_watch", "sample"]}


@router.get("/schedules")
async def schedules(_who: Principal = Depends(READ),  # noqa: B008
                    auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    async with auth.factory() as s:
        rows = (await s.scalars(select(Schedule).order_by(Schedule.data_source,
                                                          Schedule.kind))).all()
    return [schedule_view(r) for r in rows]


@router.put("/schedules")
async def set_schedule(body: NewSchedule, request: Request,
                       who: Principal = Depends(ADMIN),  # noqa: B008
                       auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            row = await add_schedule(s, body.data_source, body.kind, body.when,
                                     created_by=who.login, params=_params(body.params),
                                     known=await known_data_sources(s, auth.settings))
        except JobRefused as exc:
            raise HTTPException(400, str(exc)) from None
        view = schedule_view(row)
        await audit.record(s, "schedule_changed", actor=who.login, subject=body.data_source,
                           detail={"kind": body.kind, "when": view["when"],
                                   "params": body.params}, ip=client_ip(request))
        await s.commit()
        return view


@router.post("/schedules/{schedule_id}/enabled")
async def enable_schedule(schedule_id: uuid.UUID, body: Enabled, request: Request,
                          who: Principal = Depends(ADMIN),  # noqa: B008
                          auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        row = await s.get(Schedule, schedule_id)
        if row is None:
            raise HTTPException(404, "no such schedule")
        row.enabled = body.enabled
        await audit.record(s, "schedule_changed", actor=who.login, subject=row.data_source,
                           detail={"kind": row.kind, "enabled": body.enabled},
                           ip=client_ip(request))
        await s.commit()
        return schedule_view(row)


@router.get("/alerts")
async def alerts(_who: Principal = Depends(ADMIN),  # noqa: B008
                 auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    async with auth.factory() as s:
        return [_alert_view(a) for a in await open_alerts(s)]


@router.post("/alerts/{alert_id}/ack")
async def ack(alert_id: uuid.UUID, request: Request,
              who: Principal = Depends(ADMIN),  # noqa: B008
              auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            row = await acknowledge(s, alert_id, by=who.login)
        except JobNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        await audit.record(s, "alert_acknowledged", actor=who.login, subject=row.kind,
                           detail={"alert": str(row.id)}, ip=client_ip(request))
        await s.commit()
        return _alert_view(row)


@router.get("/{job_id}")
async def job(job_id: uuid.UUID, _who: Principal = Depends(READ),  # noqa: B008
              auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        row = await s.get(Job, job_id)
        if row is None:
            raise HTTPException(404, "no such job")
        events = (await s.scalars(select(JobEvent).where(JobEvent.job_id == job_id)
                                  .order_by(JobEvent.seq))).all()
        return job_view(row, list(events))


@router.post("", status_code=201)
async def start(body: NewJob, request: Request, who: Principal = Depends(ADMIN),  # noqa: B008
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            row = await enqueue(s, body.kind, body.data_source, requested_by=who.login,
                                params=_params(body.params),
                                known=await known_data_sources(s, auth.settings))
        except JobRefused as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "job_started", actor=who.login,
                           subject=f"{body.data_source}: {body.kind}",
                           detail={"job": str(row.id), "params": body.params},
                           ip=client_ip(request))
        await s.commit()
        return job_view(row)


@router.post("/{job_id}/cancel")
async def cancel(job_id: uuid.UUID, request: Request,
                 who: Principal = Depends(ADMIN),  # noqa: B008
                 auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            row = await request_cancel(s, job_id, by=who.login)
        except JobNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except JobRefused as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "job_cancelled", actor=who.login,
                           subject=f"{row.data_source}: {row.kind}",
                           detail={"job": str(row.id)}, ip=client_ip(request))
        await s.commit()
        return job_view(row)
