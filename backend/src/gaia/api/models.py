"""Models over HTTP (Phase 7D; P7-4, P7-5; D101) — the *Models* page.

Administrators register, probe, trial, switch, roll back and retire; auditors read. Every
change is audited with the administrator's login; a switch or a rollback also raises an alert
(``model_switched``) for every administrator. A key typed here is stored encrypted
(``GAIA_MASTER_KEY``) and never shown again.

    GET    /api/models                       admin, auditor  models, assignments, last trials
    POST   /api/models                       admin   {"name", "base_url", "model_id", "key"? |
                                                      "key_env"?, "locality", "options"?}
    POST   /api/models/{name}/probe          admin
    POST   /api/models/{name}/trial          admin   {"data_source", "purpose", "params"?}
    POST   /api/models/switch                admin   {"purpose", "model", "force"?, "note"?}
    POST   /api/models/rollback              admin   {"purpose", "note"?}
    DELETE /api/models/{name}                admin   (retire)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from gaia.auth import audit
from gaia.auth.deps import client_ip, get_auth, require
from gaia.auth.service import AuthService, Principal
from gaia.db.models.ops import MODEL_PURPOSES, Job, ModelAssignment
from gaia.jobs.queue import JobRefused, alert, enqueue, job_view
from gaia.jobs.worker import known_data_sources
from gaia.models.registry import (
    ModelError,
    add_model,
    assignments,
    get_model,
    list_models,
    meanings_by_model,
    probe_model,
    retire,
    rollback,
    switch,
)
from gaia.models.trial import trial_lines

__all__ = ["router"]

router = APIRouter(prefix="/api/models", tags=["models"])
ADMIN = require("admin")
READ = require("admin", "auditor")

TRIAL_PARAMS = {"runs", "tables", "file", "no_vectors"}


class NewModel(BaseModel):
    name: str
    base_url: str
    model_id: str
    key: str | None = None
    key_env: str | None = None
    locality: str = "remote"
    options: dict[str, Any] = Field(default_factory=dict)


class NewTrial(BaseModel):
    data_source: str
    purpose: str = "both"
    params: dict[str, Any] = Field(default_factory=dict)


class Switch(BaseModel):
    purpose: str
    model: str
    force: bool = False
    note: str | None = None


class Rollback(BaseModel):
    purpose: str
    note: str | None = None


def _assignment_view(a: ModelAssignment) -> dict[str, Any]:
    return {"purpose": a.purpose, "model": a.model_name, "previous": a.previous,
            "since": a.since, "rollback_until": a.rollback_until, "forced": a.forced,
            "by": a.by, "note": a.note,
            "trial_job_id": str(a.trial_job_id) if a.trial_job_id else None}


def _trial_view(j: Job) -> dict[str, Any]:
    view = job_view(j)
    view["lines"] = trial_lines(j.result or {}) if j.result else []
    return view


@router.get("")
async def models(_who: Principal = Depends(READ),  # noqa: B008
                 auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    settings = auth.settings
    async with auth.factory() as s:
        serving = await assignments(s)
        entries = await list_models(s, settings, include_retired=True)
        trials = (await s.scalars(select(Job).where(Job.kind == "model_trial")
                                  .order_by(Job.created_at.desc()).limit(20))).all()
        history = (await s.scalars(select(ModelAssignment)
                                   .order_by(ModelAssignment.since.desc()).limit(20))).all()
        known = sorted(await known_data_sources(s, settings))
        meanings = await meanings_by_model(s)
    served = {p: (serving[p].model_name if p in serving else settings.llm.model)
              for p in MODEL_PURPOSES}
    out = []
    for m in entries:
        view = m.view()
        view["serves"] = [p for p, n in served.items() if n == m.name]
        view["usable"] = m.usable
        out.append(view)
    return {"models": out, "purposes": list(MODEL_PURPOSES), "serving": served,
            "assignments": {p: _assignment_view(a) for p, a in serving.items()},
            "history": [_assignment_view(a) for a in history],
            "trials": [_trial_view(j) for j in trials], "data_sources": known,
            "meanings": meanings,
            "rollback_days": settings.models.rollback_days,
            "trial_valid_days": settings.models.trial_valid_days}


@router.post("", status_code=201)
async def add(body: NewModel, request: Request, who: Principal = Depends(ADMIN),  # noqa: B008
              auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            entry = await add_model(s, auth.settings, name=body.name, base_url=body.base_url,
                                    model_id=body.model_id, key=body.key or None,
                                    key_env=body.key_env or None, locality=body.locality,
                                    options=body.options, by=who.login)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - e.g. no GAIA_MASTER_KEY for a typed key
            if type(exc).__name__ == "CredentialError":
                raise HTTPException(400, str(exc)) from None
            raise
        await audit.record(s, "model_added", actor=who.login, subject=entry.name,
                           detail={"model_id": entry.model_id, "base_url": entry.base_url,
                                   "locality": entry.locality}, ip=client_ip(request))
        await s.commit()
        return entry.view()


@router.post("/switch")
async def do_switch(body: Switch, request: Request,
                    who: Principal = Depends(ADMIN),  # noqa: B008
                    auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _change(auth, who, request, "switch", body.purpose, body.model, body.force,
                         body.note)


@router.post("/rollback")
async def do_rollback(body: Rollback, request: Request,
                      who: Principal = Depends(ADMIN),  # noqa: B008
                      auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _change(auth, who, request, "rollback", body.purpose, None, False, body.note)


async def _change(auth: AuthService, who: Principal, request: Request, cmd: str, purpose: str,
                  name: str | None, force: bool, note: str | None) -> dict[str, Any]:
    if purpose not in MODEL_PURPOSES:
        raise HTTPException(400, f"purpose is one of {', '.join(MODEL_PURPOSES)}")
    async with auth.factory() as s:
        try:
            if cmd == "switch":
                row = await switch(s, auth.settings, purpose, name or "", by=who.login,
                                   force=force, note=note)
            else:
                row = await rollback(s, auth.settings, purpose, by=who.login, note=note)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "model_switched" if cmd == "switch" else "model_rolled_back",
                           actor=who.login, subject=purpose,
                           detail={"model": row.model_name, "previous": row.previous,
                                   "forced": row.forced, "note": note},
                           ip=client_ip(request))
        await alert(s, "model_switched",
                    f"{purpose} is now served by {row.model_name} (was {row.previous}, by "
                    f"{who.login})" + (" — without a trial (forced)" if row.forced else ""),
                    detail={"purpose": purpose, "model": row.model_name,
                            "previous": row.previous})
        await s.commit()
        return _assignment_view(row)


@router.post("/{name}/probe")
async def probe(name: str, request: Request, who: Principal = Depends(ADMIN),  # noqa: B008
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            entry = await probe_model(s, auth.settings, name,
                                      client_factory=getattr(request.app.state,
                                                             "model_client_factory", None))
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "model_probed", actor=who.login, subject=name,
                           detail={"status": entry.status}, ip=client_ip(request))
        await s.commit()
        return entry.view()


@router.post("/{name}/trial", status_code=201)
async def trial(name: str, body: NewTrial, request: Request,
                who: Principal = Depends(ADMIN),  # noqa: B008
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    if body.purpose not in ("answering", "reasoning", "both"):
        raise HTTPException(400, "purpose is answering, reasoning or both")
    unknown = set(body.params) - TRIAL_PARAMS
    if unknown:
        raise HTTPException(400, f"unknown parameter(s): {', '.join(sorted(unknown))} "
                                 f"({', '.join(sorted(TRIAL_PARAMS))})")
    params = {**body.params, "model": name, "purpose": body.purpose}
    async with auth.factory() as s:
        try:
            entry = await get_model(s, auth.settings, name)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        if not entry.usable:
            raise HTTPException(400, f"{name} is {entry.status}: probe it first")
        try:
            row = await enqueue(s, "model_trial", body.data_source, requested_by=who.login,
                                params=params,
                                known=await known_data_sources(s, auth.settings))
        except JobRefused as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "job_started", actor=who.login,
                           subject=f"{body.data_source}: model_trial",
                           detail={"job": str(row.id), "params": params},
                           ip=client_ip(request))
        await s.commit()
        return job_view(row)


@router.delete("/{name}")
async def remove(name: str, request: Request, who: Principal = Depends(ADMIN),  # noqa: B008
                 auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        try:
            entry = await retire(s, auth.settings, name, by=who.login)
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        await audit.record(s, "model_retired", actor=who.login, subject=name,
                           ip=client_ip(request))
        await s.commit()
        return entry.view()
