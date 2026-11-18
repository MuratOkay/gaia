"""A model trial (Phase 7D; P7-4, P6-7; D101): a candidate measured against the model in use,
before any switch — a ``model_trial`` job (``make model ARGS="trial NAME"``, the *Models* page).

**Answering** — the data source's golden set (``jobs.golden_files``, or ``file=``) run
``models.trial_runs`` times by each model, **interleaved in one session** (current, candidate,
current, …: P6-8 — a provider's drift between sessions must not pass for the models'
difference), compared question by question by P6-8's verdict (named breaks, an interval over
runs and questions). The current model's runs are ordinary measurements; the candidate's carry
the build label ``<build> + model trial: NAME`` and stay out of the ordinary report.

**Reasoning** — the tables with the most SME decisions (``models.trial_tables``) re-reasoned by
both models **in a shadow** (``ReasoningOptions.shadow``: the answers are kept nowhere), each
column's answer judged against what the SME decided, **by the same rule for both models**
(never by the LLM judge, P6-0): *wrong* when the role differs; *partly* when the name shares
less than half its words with the decided one, or the entity or unit differ; *right*
otherwise. Rejected meanings (no decided one to compare) and unused columns are left out.

**The impact preview** — what a switch would cost and take: every live table re-reasoned at
the candidate's measured tokens and seconds per table, in nights of the refresh budget; the
golden runs' tokens and latency per question; what stays (SME decisions, which carry over);
and that the candidate starts uncalibrated.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from statistics import median
from typing import Any

from sqlalchemy import func, select

from gaia.core.config import Settings
from gaia.evaluation.net import TRIAL_MARK
from gaia.evaluation.quality import wilson
from gaia.models.registry import ModelError, get_model, llm_config_of

__all__ = ["TRIAL_MARK", "judge_column", "name_words", "run_trial"]

_STOP = {"the", "a", "an", "of", "id", "code", "number", "no", "to", "for", "and", "in", "is"}


def name_words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


def judge_column(answer: Any, decided: Any) -> str:
    """right | partly | wrong: a model's answer for a column against the SME's decision."""
    if answer is None:
        return "wrong"
    if (answer.role or "") != (decided.role or ""):
        return "wrong"
    a = name_words(answer.business_name) | {w for s in (answer.synonyms or [])
                                            for w in name_words(s)}
    d = name_words(decided.business_name)
    overlap = len(a & d) / len(d) if d else 1.0
    same = lambda x, y: not x or not y or x.strip().lower() == y.strip().lower()  # noqa: E731
    if overlap < 0.5 or not same(answer.entity, decided.entity) or not same(answer.unit,
                                                                            decided.unit):
        return "partly"
    return "right"


def _share(counts: Counter) -> dict[str, Any]:
    n = sum(counts.values())
    right = counts["right"] + 0.5 * counts["partly"]
    ci = wilson(round(right), n) if n else None
    return {"n": n, "right": counts["right"], "partly": counts["partly"],
            "wrong": counts["wrong"], "share": right / n if n else None,
            "low": ci[0] if ci else None, "high": ci[1] if ci else None}


async def _decided(session, label: str, limit: int) -> dict[str, dict[str, Any]]:
    """The tables with the most SME decisions to compare with: table -> {column: decided}."""
    from gaia.db.models.catalog import ColumnRecord, DataSource, TableRecord
    from gaia.db.models.semantics import ColumnSemantic

    rows = (await session.execute(
        select(ColumnSemantic, ColumnRecord.column_name, TableRecord.schema_name,
               TableRecord.object_name)
        .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
        .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
        .join(DataSource, DataSource.id == TableRecord.data_source_id)
        .where(DataSource.name == label, ColumnSemantic.is_current.is_(True),
               ColumnSemantic.reviewed_by.is_not(None),
               ColumnSemantic.status == "sme_reviewed", ColumnSemantic.unused.is_(False),
               TableRecord.is_tombstoned.is_(False), TableRecord.kind == "table"))).all()
    by_table: dict[str, dict[str, Any]] = {}
    for sem, col, schema, table in rows:
        if ((sem.score_breakdown or {}).get("sme") or {}).get("action") not in ("accept",
                                                                                 "correct"):
            continue
        by_table.setdefault(f"{schema}.{table}", {})[col] = sem
    top = sorted(by_table, key=lambda t: (-len(by_table[t]), t))[:limit]
    return {t: by_table[t] for t in top}


async def _shadow(ctx, settings: Settings, label: str, tables: list[str], client_for
                  ) -> dict[str, dict[str, Any]]:
    from gaia.semantics.service import ReasoningOptions, run_reasoning

    async with ctx.factory() as s:
        rep = await run_reasoning(settings, s, ReasoningOptions(
            target=label, tables=tables, shadow=True, force=True, embed=False),
            llm_factory=client_for)
    return rep.shadow


def _judge(shadow: dict[str, dict[str, Any]], decided: dict[str, dict[str, Any]]
           ) -> tuple[Counter, list[dict[str, Any]], dict[str, float]]:
    counts: Counter = Counter()
    misses: list[dict[str, Any]] = []
    tokens = secs = 0.0
    for table, cols in decided.items():
        got = shadow.get(table)
        outcome = got["outcome"] if got else None
        answers = {c.name: c for c in (outcome.reasoning.columns if outcome and
                                       outcome.reasoning else [])}
        if got:
            secs += got["seconds"]
            tokens += sum(c.usage.prompt_tokens + c.usage.completion_tokens
                          for c in outcome.calls)
        for col, dec in cols.items():
            verdict = judge_column(answers.get(col), dec)
            counts[verdict] += 1
            if verdict != "right":
                a = answers.get(col)
                misses.append({"column": f"{table}.{col}", "verdict": verdict,
                               "decided": f"{dec.business_name} ({dec.role})",
                               "answer": f"{a.business_name} ({a.role})" if a else "none"})
    n = max(1, len(decided))
    return counts, misses, {"tokens_per_table": tokens / n, "seconds_per_table": secs / n}


def _side(runs: list[Any]) -> dict[str, Any]:
    items = [i for r in runs for i in r.items.values() if i.correct is not None]
    secs = [i.seconds for r in runs for i in r.items.values() if i.seconds]
    return {"runs": [r.id for r in runs], "correct": sum(1 for i in items if i.correct),
            "scored": len(items),
            "tokens_per_run": sum(i.tokens for r in runs for i in r.items.values())
            / max(1, len(runs)),
            "seconds_per_question": median(secs) if secs else None}


async def run_trial(ctx) -> dict[str, Any]:
    """The ``model_trial`` job: ``params`` — ``model`` (required), ``purpose`` (answering,
    reasoning or both — the default), ``runs``, ``tables``, ``file``."""
    from gaia.evaluation.golden_exec import (
        GoldenOptions,
        GoldenRefused,
        execute_golden,
        resolve_path,
    )
    from gaia.evaluation.quality import compare
    from gaia.evaluation.store import load_runs
    from gaia.jobs.kinds import Refused

    settings, job, label = ctx.settings, ctx.job, ctx.job.data_source
    params = job.params or {}
    name = params.get("model")
    purpose = params.get("purpose", "both")
    if purpose not in ("answering", "reasoning", "both"):
        raise Refused("purpose is answering, reasoning or both")
    async with ctx.factory() as s:
        try:
            cand = await get_model(s, settings, name or "")
        except ModelError as exc:
            raise Refused(str(exc)) from None
    if not cand.usable:
        raise Refused(f"{cand.name} is {cand.status}: probe it first")
    cand_llm = llm_config_of(cand, settings)
    result: dict[str, Any] = {"model": cand.name, "model_id": cand.model_id,
                              "purpose": purpose, "locality": cand.locality}

    def settings_with(cfg):
        return settings.model_copy(update={"llm": cfg})

    # -- answering ---------------------------------------------------------------------------
    if purpose in ("answering", "both"):
        current = ctx.settings_for("answering")
        result["answering"] = {"against": current.llm.model}
        file = params.get("file") or settings.jobs.golden_files.get(label)
        same = (current.llm.model == cand.model_id and current.llm.base_url == cand.base_url)
        if same and purpose == "answering":
            raise Refused(f"{cand.name} answers already")
        if same:
            result["answering"]["skipped"] = f"{cand.name} answers already"
        elif not file or not resolve_path(file).is_file():
            if purpose == "answering":
                raise Refused(f"no golden set for {label}: give file=…")
            result["answering"]["skipped"] = f"no golden set for {label}"
        else:
            runs = max(1, int(params.get("runs", settings.models.trial_runs)))
            base = resolve_path(settings.jobs.runs_dir) / f"{label}_trial_{job.id.hex[:8]}"
            made: dict[str, list[str]] = {"current": [], "candidate": []}
            for i in range(1, runs + 1):
                for side, cfg in (("current", current.llm), ("candidate", cand_llm)):
                    if ctx.signal.reason():
                        break
                    ctx.step("golden", side=side, run=i, of=runs)
                    lines: list[str] = []
                    try:
                        ex = await execute_golden(
                            settings_with(cfg), ctx.factory,
                            GoldenOptions(file=str(file), out=str(base / f"{side}{i}"),
                                          target=label, no_vectors=bool(params.get(
                                              "no_vectors")),
                                          build_suffix=(f"{TRIAL_MARK}{cand.name}"
                                                        if side == "candidate" else None)),
                            llm_inner=ctx.client(cfg),
                            echo=lambda *a, _l=lines, **_k: _l.append(" ".join(map(str, a))),
                            created_by=f"job:{str(job.id)[:8]}")
                    except GoldenRefused as exc:
                        raise Refused(str(exc)) from None
                    made[side].append(str(ex.run.id))
                    await ctx.log(f"golden run {i} of {runs}, {side} ({cfg.model}): "
                                  f"{round((ex.summary.get('correct') or 0) * 100)}% of "
                                  f"{ex.summary.get('with_reference')}")
            async with ctx.factory() as s:
                views = {r.id: r for r in await load_runs(s, label)}
            base_runs = [views[i] for i in made["current"] if i in views]
            cand_runs = [views[i] for i in made["candidate"] if i in views]
            if base_runs and cand_runs:
                c = compare(base_runs, cand_runs)
                result["answering"].update({
                    "verdict": c.verdict, "difference": c.difference, "low": c.low,
                    "high": c.high, "questions": c.questions, "breaks": c.breaks,
                    "fixes": c.fixes, "current_runs": _side(base_runs),
                    "candidate_runs": _side(cand_runs)})
                await ctx.log(f"answering: {c.verdict}, {c.difference * 100:+.1f} points "
                              f"({c.low * 100:+.1f} to {c.high * 100:+.1f}) over {c.questions} "
                              "question(s)" + (f"; broken: {', '.join(c.breaks)}"
                                               if c.breaks else ""))

    # -- reasoning ---------------------------------------------------------------------------
    if purpose in ("reasoning", "both") and not ctx.signal.reason():
        current = ctx.settings_for("reasoning")
        result["reasoning"] = {"against": current.llm.model}
        same = (current.llm.model == cand.model_id and current.llm.base_url == cand.base_url)
        if same and purpose == "reasoning":
            raise Refused(f"{cand.name} reasons already")
        decided: dict[str, dict[str, Any]] = {}
        if not same:
            async with ctx.factory() as s:
                decided = await _decided(s, label, int(params.get(
                    "tables", settings.models.trial_tables)))
        if same:
            result["reasoning"]["skipped"] = f"{cand.name} reasons already"
        elif not decided:
            result["reasoning"]["skipped"] = ("no SME decision to compare with on "
                                              f"{label}: review some columns first")
            if purpose == "reasoning":
                raise Refused(result["reasoning"]["skipped"])
        else:
            tables = sorted(decided)
            ctx.step("shadow", tables=len(tables))
            sides: dict[str, Any] = {}
            for side, cfg in (("current", current.llm), ("candidate", cand_llm)):
                if ctx.signal.reason():
                    break
                shadow = await _shadow(ctx, settings_with(cfg), label, tables,
                                       lambda _c=cfg: ctx.client(_c))
                counts, misses, cost = _judge(shadow, decided)
                sides[side] = {**_share(counts), **cost, "misses": misses[:30]}
                await ctx.log(f"meanings, {side} ({cfg.model}): right {counts['right']}, "
                              f"partly {counts['partly']}, wrong {counts['wrong']} of "
                              f"{sum(counts.values())} SME-decided column(s)")
            result["reasoning"].update({"tables": tables, **sides})

    # -- the impact preview --------------------------------------------------------------------
    async with ctx.factory() as s:
        result["preview"] = await _preview(s, settings, label, cand, result)
    if ctx.signal.stopped:
        result["stopped"] = ctx.signal.stopped
    return result


async def _preview(session, settings: Settings, label: str, cand, result: dict[str, Any]
                   ) -> dict[str, Any]:
    from gaia.db.models.catalog import DataSource, TableRecord
    from gaia.db.models.evaluation import Calibrator
    from gaia.db.models.semantics import ColumnSemantic

    ds = await session.scalar(select(DataSource.id).where(DataSource.name == label))
    tables = await session.scalar(select(func.count()).select_from(TableRecord).where(
        TableRecord.data_source_id == ds, TableRecord.is_tombstoned.is_(False))) or 0
    decisions = await session.scalar(
        select(func.count()).select_from(ColumnSemantic)
        .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
        .where(TableRecord.data_source_id == ds, ColumnSemantic.is_current.is_(True),
               ColumnSemantic.reviewed_by.is_not(None))) or 0
    calibrated = (await session.scalars(select(Calibrator.kind).where(
        Calibrator.model == settings.calibration.canonical(cand.model_id),
        Calibrator.in_use.is_(True)))).all()
    out: dict[str, Any] = {"tables": tables, "sme_decisions_kept": decisions,
                           "calibrated": sorted(calibrated)}
    cand_r = (result.get("reasoning") or {}).get("candidate") or {}
    if cand_r.get("tokens_per_table"):
        total = cand_r["tokens_per_table"] * tables
        budget = settings.jobs.refresh_token_budget
        out["reasoning"] = {
            "tokens": round(total),
            "nights": math.ceil(total / budget) if budget else 1,
            "hours": round(cand_r["seconds_per_table"] * tables
                           / max(1, settings.llm.concurrency) / 3600, 1)}
        price_in = cand.options.get("price_in_per_million")
        if price_in:
            out["reasoning"]["cost"] = round(total / 1e6 * float(price_in), 2)
    ans = (result.get("answering") or {}).get("candidate_runs")
    if ans:
        out["answering"] = {"tokens_per_golden_run": round(ans["tokens_per_run"]),
                            "seconds_per_question": ans["seconds_per_question"]}
    return out


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def trial_lines(result: dict[str, Any]) -> list[str]:
    """A trial's result in words (``make model ARGS="show"``)."""
    out = [f"trial of {result.get('model')} ({result.get('model_id')}, "
           f"{result.get('locality')}) — {result.get('purpose')}"]
    a = result.get("answering") or {}
    if a.get("skipped"):
        out.append(f"  answering: not measured — {a['skipped']}")
    elif a.get("verdict"):
        cur, cand = a["current_runs"], a["candidate_runs"]
        out += [f"  answering against {a['against']}: **{a['verdict']}**, "
                f"{a['difference'] * 100:+.1f} points ({a['low'] * 100:+.1f} to "
                f"{a['high'] * 100:+.1f}) over {a['questions']} question(s)"
                + (f"; broken: {', '.join(a['breaks'])}" if a.get("breaks") else "")
                + (f"; fixed: {', '.join(a['fixes'])}" if a.get("fixes") else ""),
                f"    current: {cur['correct']} of {cur['scored']} right, "
                f"{cur['tokens_per_run']:,.0f} tokens a run, "
                f"{cur['seconds_per_question'] or 0:.1f} s a question (median)",
                f"    candidate: {cand['correct']} of {cand['scored']} right, "
                f"{cand['tokens_per_run']:,.0f} tokens a run, "
                f"{cand['seconds_per_question'] or 0:.1f} s a question (median)"]
    r = result.get("reasoning") or {}
    if r.get("skipped"):
        out.append(f"  meanings: not measured — {r['skipped']}")
    elif r.get("candidate"):
        out.append(f"  meanings against the SME's decisions, {len(r['tables'])} table(s):")
        for side in ("current", "candidate"):
            x = r.get(side)
            if not x:
                continue
            who = r.get("against") if side == "current" else result.get("model")
            out.append(f"    {side} ({who}): right {x['right']}, partly {x['partly']}, wrong "
                       f"{x['wrong']} of {x['n']} = {_pct(x['share'])} (95% interval "
                       f"{_pct(x['low'])}–{_pct(x['high'])}); "
                       f"{x['tokens_per_table']:,.0f} tokens and "
                       f"{x['seconds_per_table']:.0f} s a table")
        for m in (r.get("candidate") or {}).get("misses", [])[:8]:
            out.append(f"      {m['verdict']}: {m['column']} — decided {m['decided']}, "
                       f"answered {m['answer']}")
    p = result.get("preview") or {}
    if p:
        out.append("  if switched:")
        if p.get("reasoning"):
            pr = p["reasoning"]
            out.append(f"    re-reasoning {p['tables']} table(s): ~{pr['tokens']:,} tokens, "
                       f"~{pr['nights']} night(s) of the refresh budget, ~{pr['hours']} h"
                       + (f", ~{pr['cost']} in the model's prices" if pr.get("cost") else ""))
        if p.get("answering"):
            pa = p["answering"]
            out.append(f"    answering: {pa['tokens_per_golden_run']:,} tokens a golden run, "
                       f"{pa['seconds_per_question'] or 0:.1f} s a question")
        out.append(f"    kept: {p.get('sme_decisions_kept', 0)} SME decision(s) on "
                   "unchanged columns; the meanings and runs of the model in use, read-only")
        out.append("    starts uncalibrated" if not p.get("calibrated") else
                   f"    calibrators in use for it: {', '.join(p['calibrated'])}")
    if result.get("stopped"):
        out.append(f"  stopped early: {result['stopped']}")
    return out
