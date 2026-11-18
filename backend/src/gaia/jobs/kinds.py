"""What each kind of job does (Phase 7C, P5-12, P7-7; D98).

**refresh** — the daily workflow of USER_GUIDE §4, chained, for one data source: introspect
(read the catalog, diff, persist) → profile what needs it (P3-6: a changed structure, a moved
row count, an old profile; the rest reused without a data query) → reason what changed (the
skip key; an unchanged table costs nothing) and embed → test the stored claims (Stage 3) →
reason again what the tests changed. Each step is the existing service, with D67's lock; the
job adds a **window** (the data source's run window, A5 — a job is refused outside it, and
stops between tables when it closes), a **token budget** (``jobs.refresh_token_budget``, P7-7)
and **cancellation**, all read between tables through one ``StopSignal``. Stopped early, it is
*partial*: what it finished is kept, and the next run takes up the rest — nothing was
recorded for it.

**drift_watch** — the golden set of the data source (``jobs.golden_files``), run fresh
``jobs.drift_runs`` times (three: P6-8), then compared with the latest ordinary runs before it
by P6-8's verdict on the questions both share: a **named break** or *fails* raises a
``drift`` alert; a model id the provider reports that the baseline never saw raises
``model_changed`` ("always the latest" moved on, §21f).

**sample** — the week's calibration sample (7A), drawn without waiting for an SME to open
*Review*.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import Settings
from gaia.db.locks import DataSourceBusy
from gaia.db.models.ops import Job
from gaia.jobs.signal import MeteredLLM, StopSignal

__all__ = ["JobContext", "JobDeps", "JobOutcome", "KINDS", "Refused"]


class Refused(RuntimeError):
    """The job cannot start (busy, outside its window, nothing to run)."""


@dataclass
class JobDeps:
    """What a job calls out to — the real services by default; tests put scripted ones in."""

    llm_factory: Callable[[], Any] | None = None  # the reasoning / answering model
    embedder_factory: Callable[[], Any] | None = None
    profiler_factory: Callable[..., Any] | None = None
    clock: Callable[[], datetime] | None = None  # windows (tests)


@dataclass
class JobContext:
    settings: Settings
    factory: async_sessionmaker
    job: Job
    signal: StopSignal
    deps: JobDeps
    log: Callable[[str], Awaitable[None]]
    progress: dict[str, Any] = field(default_factory=dict)

    def step(self, name: str, **info: Any) -> None:
        self.progress.clear()
        self.progress.update({"step": name, **info})

    def llm(self) -> Any:
        """The model client, metered into the job's budget."""
        if self.deps.llm_factory is not None:
            inner = self.deps.llm_factory()
        else:
            from gaia.tools.ask import llm_client

            inner = llm_client(self.settings, None)
        return MeteredLLM(inner, self.signal)

    def embedder(self) -> Any:
        if self.deps.embedder_factory is not None:
            return self.deps.embedder_factory()
        from gaia.llm.cache import build_cache
        from gaia.llm.embeddings import build_embedding_client

        cache = build_cache(self.settings.cache.redis_url, enabled=True)
        return build_embedding_client(self.settings.embeddings, cache=cache,
                                      cache_ttl_s=self.settings.cache.ttl_s.embedding)


@dataclass
class JobOutcome:
    state: str  # done | partial | refused
    result: dict[str, Any]
    error: str | None = None


def window_of(settings: Settings, label: str) -> str | None:
    """The data source's run window (A5): its own, else ``profiling.window``."""
    try:
        target = settings.target(label)
    except Exception:  # noqa: BLE001 - an unusable data source: the step itself will say so
        return settings.profiling.window
    return target.profiling_window or settings.profiling.window


# -- refresh -----------------------------------------------------------------------------------


def _counts(items: list[Any], attr: str = "action") -> dict[str, int]:
    out: dict[str, int] = {}
    for r in items:
        key = getattr(r, attr)
        out[key] = out.get(key, 0) + 1
    return out


async def refresh(ctx: JobContext) -> JobOutcome:
    from gaia.introspect.service import run_introspection
    from gaia.profiling.service import (
        ProfileOptions,
        ProfilingError,
        profiling_enabled,
        run_profiling,
        run_stage3,
    )
    from gaia.semantics.service import ReasoningOptions, run_reasoning

    settings, label, sig = ctx.settings, ctx.job.data_source, ctx.signal
    params = ctx.job.params or {}
    sig.budget_tokens = int(params.get("budget", settings.jobs.refresh_token_budget))
    window = None if params.get("ignore_window") else window_of(settings, label)
    sig.window = window
    if sig.reason():
        raise Refused(f"outside the run window {window}: the refresh waits for it "
                      "(or start it with ignore_window)")
    result: dict[str, Any] = {"window": window, "token_budget": sig.budget_tokens or None}
    now = ctx.deps.clock() if ctx.deps.clock else None

    # 1. the catalog: always cheap, always full
    ctx.step("introspect")
    async with ctx.factory() as s:
        run = await run_introspection(settings, s, target_name=label)
        await s.commit()
    summary = run.diff.summary()
    result["introspect"] = {**summary, "first": run.is_first_capture,
                            "changes": [c.summary() for c in run.diff.changes[:20]]}
    await ctx.log(f"introspect: {summary['added']} added, {summary['structure_changed']} "
                  f"changed in structure, {summary['data_changed']} in data, "
                  f"{summary['removed']} removed, {summary['unchanged']} unchanged"
                  + (" (first capture)" if run.is_first_capture else "")
                  + "".join(f"\n  - {c.summary()}" for c in run.diff.changes[:20]))

    # 2. evidence from the data, where it is due
    profiled = profiling_enabled(settings, label)
    if profiled and not sig.reason():
        ctx.step("profile")
        try:
            async with ctx.factory() as s:
                rep = await run_profiling(settings, s, ProfileOptions(
                    target=label, ignore_window=True, stop=sig),
                    profiler_factory=ctx.deps.profiler_factory, now=now)
        except (DataSourceBusy, ProfilingError) as exc:
            raise Refused(str(exc)) from None
        result["profile"] = _counts(rep.results)
        await ctx.log("profile: " + _say(result["profile"]))
    elif not profiled:
        await ctx.log("profile: profiling is off for this data source (catalog evidence only)")

    # 3. meanings, where their inputs changed — and their embeddings
    async def reason(step: str) -> None:
        if sig.reason():
            return
        ctx.step(step)
        embedder = ctx.embedder()
        try:
            async with ctx.factory() as s:
                rep = await run_reasoning(settings, s, ReasoningOptions(target=label, stop=sig),
                                          llm_factory=ctx.llm, embedder=embedder)
        except DataSourceBusy as exc:
            raise Refused(str(exc)) from None
        finally:
            close = getattr(embedder, "close", None)
            if close:
                close()
        result[step] = {**_counts(rep.results), "embeddings": rep.embeddings_written}
        await ctx.log(f"{step}: " + _say(result[step]))

    await reason("reason")

    # 4. the stored claims tested on the data, then meanings again where a test changed them
    if profiled and settings.jobs.refresh_claim_tests and not params.get("no_claim_tests"):
        if not sig.reason():
            ctx.step("claim_tests")
            async with ctx.factory() as s:
                try:
                    rep = await run_stage3(settings, s, ProfileOptions(
                        target=label, ignore_window=True, stop=sig),
                        profiler_factory=ctx.deps.profiler_factory, now=now)
                except ProfilingError as exc:
                    raise Refused(str(exc)) from None
            result["claim_tests"] = _counts(rep.results)
            await ctx.log("claim tests: " + _say(result["claim_tests"]))
        await reason("reason_after_tests")

    result["tokens"] = sig.used_tokens
    if sig.stopped:
        result["stopped"] = sig.stopped
        await ctx.log(f"stopped: {sig.stopped} — what is done is kept; the next run takes up "
                      "the rest")
        return JobOutcome("partial", result)
    return JobOutcome("done", result)


def _say(counts: dict[str, Any]) -> str:
    return ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing to do"


# -- the drift watch -----------------------------------------------------------------------------


def _baseline(runs: list[Any], candidate_ids: set[str], n: int) -> list[Any]:
    """The latest ``n`` ordinary measured runs made before the candidate (the operator's,
    not regressions, not replays) — of the candidate's question set when there are enough,
    else of the newest set they come from (compared on shared questions)."""
    from gaia.evaluation.net import is_regression

    cand = [r for r in runs if r.id in candidate_ids]
    if not cand:
        return []
    first = min(r.started_at for r in cand)
    before = [r for r in runs if r.id not in candidate_ids and r.measured and not r.as_login
              and not is_regression(r.build) and r.started_at < first]
    same = [r for r in before if r.set_key == cand[0].set_key]
    if len(same) >= n:
        return same[-n:]
    if not before:
        return []
    newest_set = before[-1].set_key
    return [r for r in before if r.set_key == newest_set][-n:]


async def drift_watch(ctx: JobContext) -> JobOutcome:
    from gaia.evaluation.golden_exec import (
        GoldenOptions,
        GoldenRefused,
        execute_golden,
        resolve_path,
    )
    from gaia.evaluation.quality import compare
    from gaia.evaluation.store import load_runs
    from gaia.jobs.queue import alert

    settings, label, job = ctx.settings, ctx.job.data_source, ctx.job
    params = job.params or {}
    file = params.get("file") or settings.jobs.golden_files.get(label)
    if not file or not resolve_path(file).is_file():
        raise Refused(f"no golden set for {label} (jobs.golden_files, or params file=…)")
    runs = max(1, int(params.get("runs", settings.jobs.drift_runs)))
    base = resolve_path(settings.jobs.runs_dir) / f"{label}_{job.id.hex[:8]}"
    made: list[str] = []
    answered: dict[str, int] = {}
    for i in range(1, runs + 1):
        if ctx.signal.reason():
            break
        ctx.step("golden", run=i, of=runs)
        lines: list[str] = []
        try:
            execution = await execute_golden(
                settings, ctx.factory,
                GoldenOptions(file=str(file), out=str(base / f"run{i}"), target=label,
                              no_vectors=bool(params.get("no_vectors"))),
                llm_inner=ctx.llm(),
                echo=lambda *a, _l=lines, **_k: _l.append(" ".join(map(str, a))),
                created_by=f"job:{str(job.id)[:8]}")
        except GoldenRefused as exc:
            raise Refused(str(exc)) from None
        s = execution.summary
        made.append(str(execution.run.id))
        for k, v in (s.get("answered_by") or {}).items():
            answered[k] = answered.get(k, 0) + v
        await ctx.log(f"golden run {i} of {runs}: {round((s.get('correct') or 0) * 100)}% "
                      f"correct of {s.get('with_reference')} ({str(execution.run.id)[:8]})")
    result: dict[str, Any] = {"file": str(file), "runs": made, "answered_by": answered,
                              "tokens": ctx.signal.used_tokens}
    if len(made) < runs:
        result["stopped"] = ctx.signal.stopped
        return JobOutcome("partial", result, None)

    ctx.step("compare")
    async with ctx.factory() as s:
        views = await load_runs(s, label)
        candidate = [r for r in views if r.id in set(made)]
        baseline = _baseline(views, set(made), runs)
        if not baseline:
            result["verdict"] = "no baseline"
            await ctx.log("no earlier runs to compare with: these runs are the baseline of the "
                          "next watch")
            return JobOutcome("done", result)
        c = compare(baseline, candidate, history=[r for r in views if r not in candidate])
        result.update({"verdict": c.verdict, "difference": c.difference, "low": c.low,
                       "high": c.high, "questions": c.questions, "breaks": c.breaks,
                       "fixes": c.fixes, "reworded": c.reworded,
                       "baseline": [r.id for r in baseline],
                       "baseline_builds": sorted({r.build or "—" for r in baseline})})
        await ctx.log(f"against {len(baseline)} earlier run(s): {c.verdict}, "
                      f"{c.difference * 100:+.1f} points ({c.low * 100:+.1f} to "
                      f"{c.high * 100:+.1f}) over {c.questions} question(s)"
                      + (f"; named breaks: {', '.join(c.breaks)}" if c.breaks else ""))
        if c.verdict == "fails":
            await alert(s, "drift", f"the golden set of {label} got worse: "
                        + (f"broken {', '.join(c.breaks)}; " if c.breaks else "")
                        + f"{c.difference * 100:+.1f} points ({c.low * 100:+.1f} to "
                          f"{c.high * 100:+.1f})", data_source=label, job_id=job.id,
                        detail={"breaks": c.breaks, "baseline": result["baseline"],
                                "candidate": made})
        seen = set().union(*(set(r.answered_by) for r in baseline))
        new = sorted(set(answered) - seen)
        if seen and new:
            result["model_changed"] = {"new": new, "before": sorted(seen)}
            await alert(s, "model_changed", f"{label}: the provider now answers as "
                        f"{', '.join(new)} (before: {', '.join(sorted(seen))}) — a new model "
                        "starts uncalibrated; read the watch's comparison", data_source=label,
                        job_id=job.id, detail=result["model_changed"])
            await ctx.log(f"the provider reports a model the baseline never saw: {new}")
        await s.commit()
    return JobOutcome("done", result)


# -- the calibration sample ------------------------------------------------------------------------


async def sample(ctx: JobContext) -> JobOutcome:
    from sqlalchemy import select

    from gaia.db.models.catalog import DataSource
    from gaia.semantics.sampling import draw, sample_status

    label = ctx.job.data_source
    ctx.step("sample")
    async with ctx.factory() as s:
        ds = await s.scalar(select(DataSource.id).where(DataSource.name == label))
        if ds is None:
            raise Refused(f"no data source {label!r} in the metadata store (refresh it first)")
        got = await draw(s, ds, cfg=ctx.settings.calibration,
                         now=ctx.deps.clock() if ctx.deps.clock else None)
        await s.commit()
        status = await sample_status(s, ds)
    result = {"period": str(got["period"]), "new": got["new"], "drawn": got["drawn"],
              "short": got.get("short") or {}, "waiting": status["open"]}
    await ctx.log(f"the week from {got['period']}: "
                  + (f"{got['drawn']} drawn now" if got["new"] else
                     f"drawn before ({status['this_week']['drawn']})"))
    return JobOutcome("done", result)


KINDS: dict[str, Callable[[JobContext], Awaitable[JobOutcome]]] = {
    "refresh": refresh, "drift_watch": drift_watch, "sample": sample}
