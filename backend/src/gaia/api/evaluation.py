"""The evaluation store over HTTP (Phase 6A, D80) — administrators and auditors, read-only.

    GET /api/evaluation/data-sources                       data sources with golden runs
    GET /api/evaluation/runs?data_source=                  every run, fresh or replayed
    GET /api/evaluation/report?data_source=&last=&compare_builds=
                                                           the quality report (as make quality)

The same numbers as ``make quality``: measured runs pooled with 95 % intervals, the flaky
questions, the clarification measurements, and — with ``compare_builds`` — the newest build's
runs against the build before, question by question, with the meanings check of D81.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from gaia.auth.deps import _refuse, get_auth, principal
from gaia.auth.service import AuthService, Principal
from gaia.db.models.evaluation import EvaluationRun
from gaia.evaluation.quality import quality_report, select_runs
from gaia.evaluation.sections import extra_sections
from gaia.evaluation.store import load_runs

__all__ = ["router"]

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])


async def _reader(request: Request, who: Principal = Depends(principal),  # noqa: B008
                  auth: AuthService = Depends(get_auth)) -> AuthService:  # noqa: B008
    if not who.has("admin", "auditor"):
        await _refuse(auth, request, who, "admin or auditor")
    return auth


@router.get("/data-sources")
async def data_sources(auth: AuthService = Depends(_reader)) -> list[str]:  # noqa: B008
    async with auth.factory() as s:
        return sorted(set((await s.scalars(select(EvaluationRun.data_source))).all()))


@router.get("/runs")
async def runs(data_source: str, auth: AuthService = Depends(_reader)  # noqa: B008
               ) -> list[dict[str, Any]]:
    async with auth.factory() as s:
        found = await load_runs(s, data_source)
    out = []
    for r in reversed(found):
        scored = [i for i in r.items.values() if i.correct is not None]
        out.append({"id": r.id, "started_at": r.started_at, "build": r.build, "model": r.model,
                    "mode": r.mode, "as_login": r.as_login, "measured": r.measured,
                    "set_key": r.set_key, "meanings_key": r.meanings_key,
                    "sme_decisions": r.meanings.get("sme_decisions"),
                    "correct": sum(1 for i in scored if i.correct), "scored": len(scored)})
    return out


@router.get("/report")
async def report(data_source: str, last: int = 3, compare_builds: bool = False,
                 auth: AuthService = Depends(_reader)) -> dict[str, Any]:  # noqa: B008
    async with auth.factory() as s:
        found = await load_runs(s, data_source)
        extra, extra_doc = await extra_sections(s, data_source, auth.settings, auth.factory)
    sel = select_runs(found, last=max(1, min(last, 50)), compare_builds=compare_builds)
    text, doc = quality_report(sel.shown, data_source=data_source, question_set_key=sel.set_key,
                               excluded=sel.excluded, baseline=sel.baseline,
                               candidate=sel.candidate, regressions=sel.regressions,
                               history=sel.history, trials=sel.trials)
    return {**doc, **extra_doc, "markdown": text + extra}
