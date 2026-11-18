"""The quality report (Phase 6A, D80): golden runs as measurements, with their uncertainty.

One golden run of 30 questions cannot tell a regression of a few points from the model's
run-to-run variation: the D72 prompt gave 97, 90 and 93 % on three fresh runs (OWNER_VERIFICATION
§26c F6). So the report pools **several runs**, gives every proportion a 95 % **Wilson**
interval, shows which questions are right always, sometimes ("flaky") or never, and compares two
sets of runs **question by question**: the mean of the per-question differences, with a 95 %
interval from a seeded **paired bootstrap** over the questions. P6-2: a candidate passes when the
lower bound of that difference is above −3 points; fails when the upper bound is below it; else
the runs cannot tell, and more are needed.

P6-8 (2026-11-18, after the D88 host run) made the verdict two-part. **Named breaks**: a question
right in every baseline run and wrong in every candidate run (three runs a side at least) fails
the candidate by itself — a deterministic break of even one question is what a regression looks
like, and an always-right question does not flip by chance (on the host's flakiness a false
alarm is ~3 % a comparison). **The overall difference**: its interval resamples the runs as well
as the questions (a bootstrap over questions alone was over-confident when few questions moved,
and blind to what a whole run shares — §30b-6). A comparison also says how far apart in time
the two sides were made: the provider may change its model in between.

Replays are never measurements (they reproduce a run); they are listed and left out.

Phase 7C (D98): a golden set is edited — a question reworded, drafts accepted a few at a time
— and every edit makes a new question set. A comparison therefore reads the questions **both
sides share with the same wording**: an id whose question text differs between the sides is
left out and named, so a baseline survives an edit of the set without comparing two different
questions under one id. A comparison of builds may take its baseline from an earlier set.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

from gaia.evaluation.net import is_regression, is_trial, regressed_build

__all__ = ["Comparison", "ItemView", "RunView", "Selection", "compare", "confidence_levels",
           "known_flaky", "named_breaks", "quality_report", "select_runs", "shared_questions",
           "wilson", "wrong_causes"]

THRESHOLD = -0.03  # P6-2: a change may not cost more than 3 points (lower bound)
MIN_RUNS_FOR_BREAKS = 3  # P6-8: a break is named only with this many runs a side
HISTORY_RUNS = 12  # D93: the ordinary runs before a candidate that say which questions are flaky
SESSION_HOURS = 6.0  # P6-8: sides made further apart may differ by the provider's model
Z = 1.959964


@dataclass
class ItemView:
    key: str
    expect: str | None = None
    correct: bool | None = None
    executable: bool | None = None
    first_status: str | None = None
    final_status: str | None = None
    best_guess: str | None = None
    over_clarified: bool = False
    answered_directly: bool = False
    calls: int = 0
    tokens: int = 0
    seconds: float = 0.0
    reason: str | None = None
    sql: str | None = None
    detail: dict[str, Any] | None = None  # D84: what the final answer was made of
    repairs: int = 0
    question: str | None = None  # D98: the wording asked (None for older imported runs)


@dataclass
class RunView:
    id: str
    started_at: datetime
    build: str | None
    model: str | None
    mode: str
    as_login: str | None = None
    items: dict[str, ItemView] = field(default_factory=dict)
    set_key: str = ""
    meanings_key: str | None = None  # D81: the meanings it answered with (None: unknown)
    meanings: dict[str, Any] = field(default_factory=dict)
    # P6-8: a deliberate regression's questions expected to break (``drop:``), when known
    expected_breaks: list[str] | None = None
    # D98: the model ids the provider says answered (``CallRecord.answered_by``) and how often
    answered_by: dict[str, int] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        return self.mode != "replay"


def wilson(k: int, n: int) -> tuple[float, float] | None:
    """The 95 % Wilson score interval of k successes in n (None for n = 0)."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / d
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _share(k: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = wilson(k, n) or (0.0, 0.0)
    return f"{k} of {n} = {k / n:.0%} (95% interval {lo:.0%}–{hi:.0%})"


def _rate(runs: Sequence[RunView], key: str) -> float | None:
    """The share of the runs in which question ``key`` was right (scored runs only)."""
    scored = [r.items[key].correct for r in runs
              if key in r.items and r.items[key].correct is not None]
    return sum(scored) / len(scored) if scored else None


@dataclass
class Comparison:
    difference: float
    low: float
    high: float
    questions: int
    verdict: str  # passes | fails | cannot tell
    moved: list[tuple[str, float, float]]  # question, baseline rate, candidate rate
    breaks: list[str] = field(default_factory=list)  # right in every baseline run, wrong in all
    fixes: list[str] = field(default_factory=list)  # the other way round
    gap_hours: float | None = None  # from the baseline's last run to the candidate's first
    # D93: a full flip of a question known to be flaky — shown, but it decides nothing alone:
    # question -> the builds in which it was right in some runs and wrong in others
    flaky_flips: dict[str, list[str]] = field(default_factory=dict)
    # D98: ids on both sides whose question wording differs — left out of the comparison
    reworded: list[str] = field(default_factory=list)
    only_one_side: int = 0  # questions one side has and the other does not


def shared_questions(baseline: Sequence[RunView], candidate: Sequence[RunView]
                     ) -> tuple[list[str], list[str], int]:
    """(keys both sides have with the same wording, keys reworded, keys on one side only)."""
    b_keys = set().union(*(r.items for r in baseline)) if baseline else set()
    c_keys = set().union(*(r.items for r in candidate)) if candidate else set()
    both = b_keys & c_keys
    reworded = []
    for k in sorted(both):
        texts = {r.items[k].question for r in [*baseline, *candidate]
                 if k in r.items and r.items[k].question}
        if len(texts) > 1:
            reworded.append(k)
    keys = sorted(both - set(reworded))
    return keys, reworded, len(b_keys ^ c_keys)


def _scores(runs: Sequence[RunView], key: str) -> list[bool]:
    return [bool(r.items[key].correct) for r in runs
            if key in r.items and r.items[key].correct is not None]


def named_breaks(baseline: Sequence[RunView], candidate: Sequence[RunView],
                 keys: Iterable[str]) -> tuple[list[str], list[str]]:
    """(breaks, fixes): questions right in every baseline run and wrong in every candidate run
    — and the reverse — each side with at least ``MIN_RUNS_FOR_BREAKS`` scored runs."""
    breaks, fixes = [], []
    for k in keys:
        b, c = _scores(baseline, k), _scores(candidate, k)
        if len(b) < MIN_RUNS_FOR_BREAKS or len(c) < MIN_RUNS_FOR_BREAKS:
            continue
        if all(b) and not any(c):
            breaks.append(k)
        elif all(c) and not any(b):
            fixes.append(k)
    return breaks, fixes


def known_flaky(runs: Iterable[RunView]) -> dict[str, list[str]]:
    """Questions right in some and wrong in other runs **of one build** (D93): within one build
    nothing but chance moves them. question -> those builds (labels, oldest first)."""
    by_build: dict[str | None, list[RunView]] = {}
    for r in sorted(runs, key=lambda r: r.started_at):
        by_build.setdefault(r.build, []).append(r)
    out: dict[str, list[str]] = {}
    for build, group in by_build.items():
        keys = set().union(*(r.items for r in group))
        for k in sorted(keys):
            got = _scores(group, k)
            if len(got) >= 2 and any(got) and not all(got):
                out.setdefault(k, []).append(build or "—")
    return out


def compare(baseline: Sequence[RunView], candidate: Sequence[RunView], *,
            iterations: int = 4000, seed: int = 7, threshold: float = THRESHOLD,
            history: Sequence[RunView] = ()) -> Comparison:
    """``history`` (D93): ordinary runs made before the candidate (the baseline's among them);
    a full flip of a question they show flaky within one build is reported, not named."""
    keys, reworded, one_side = shared_questions(baseline, candidate)
    pairs = [(k, b, c) for k in keys
             if (b := _rate(baseline, k)) is not None and (c := _rate(candidate, k)) is not None]
    if not pairs:
        return Comparison(0.0, 0.0, 0.0, 0, "cannot tell", [], reworded=reworded,
                          only_one_side=one_side)
    diffs = [c - b for _k, b, c in pairs]
    mean = sum(diffs) / len(diffs)
    # the interval: resample the runs of each side, then the questions (two-stage, P6-8)
    qkeys = [k for k, _b, _c in pairs]
    bmat = [[(r.items[k].correct if k in r.items else None) for k in qkeys] for r in baseline]
    cmat = [[(r.items[k].correct if k in r.items else None) for k in qkeys] for r in candidate]

    def rate(rows: list[list[bool | None]], j: int) -> float | None:
        got = [row[j] for row in rows if row[j] is not None]
        return sum(got) / len(got) if got else None

    rng = random.Random(seed)  # noqa: S311 - a reproducible bootstrap, not a secret
    boots = []
    for _ in range(iterations):
        bs = [rng.choice(bmat) for _ in bmat]
        cs = [rng.choice(cmat) for _ in cmat]
        total = n = 0
        for _q in qkeys:
            j = rng.randrange(len(qkeys))
            rb, rc = rate(bs, j), rate(cs, j)
            if rb is None or rc is None:
                continue
            total += rc - rb
            n += 1
        boots.append(total / n if n else 0.0)
    boots.sort()
    low, high = boots[int(0.025 * iterations)], boots[int(0.975 * iterations) - 1]
    breaks, fixes = named_breaks(baseline, candidate, qkeys)
    flaky = known_flaky([*history, *baseline])
    flaky_flips = {k: flaky[k] for k in [*breaks, *fixes] if k in flaky}
    breaks = [k for k in breaks if k not in flaky]
    fixes = [k for k in fixes if k not in flaky]
    verdict = ("fails" if breaks or high < threshold else
               "passes" if low > threshold else "cannot tell")
    moved = [(k, b, c) for k, b, c in pairs if abs(c - b) >= 0.5]
    gap = (min(r.started_at for r in candidate) - max(r.started_at for r in baseline)
           ).total_seconds() / 3600
    return Comparison(mean, low, high, len(pairs), verdict, moved, breaks, fixes, gap,
                      flaky_flips, reworded, one_side)


@dataclass
class Selection:
    set_key: str
    shown: list[RunView]
    excluded: list[RunView]
    baseline: list[RunView] | None = None
    candidate: list[RunView] | None = None
    regressions: list[RunView] = field(default_factory=list)  # left out: they measure the net
    history: list[RunView] = field(default_factory=list)  # D93: ordinary runs before the candidate
    trials: list[RunView] = field(default_factory=list)  # 7D: a candidate model's, left out


def _as_matches(run_as: str | None, wanted: str | None) -> bool:
    if not wanted:
        return not run_as
    if not run_as:
        return False
    w, r = wanted.lower(), run_as.lower()
    if "/" in w and "/" in r:  # both say the directory: it must be the same
        return r == w
    return r.split("/")[-1] == w.split("/")[-1]  # a run records the login (``finn``)


def select_runs(runs: Sequence[RunView], *, ids: Sequence[str] = (), set_key: str | None = None,
                last: int = 3, baseline_ids: Sequence[str] = (),
                candidate_ids: Sequence[str] = (), compare_builds: bool = False,
                as_login: str | None = None) -> Selection:
    """Which runs a report reads: one question set (the newest run's unless given); the last
    ``last`` measured runs, or the runs named; for a comparison, the baseline and candidate
    named — or, with ``compare_builds``, the newest build's runs against the build before.

    A deliberate regression (``make golden --regress``, 6E part 2) measures the net, not a build:
    it is left out of the last runs and of a comparison of builds — except when the newest runs
    are one, and then ``compare_builds`` compares them with the build they were made of (the
    net's acceptance test). Named runs are always read as named (D89).

    Runs made **as a person** (``make golden --as``) answer under that person's rights, so
    their numbers are theirs (D97, 7B): without ``as_login`` only the operator's runs are
    read; with it, only that person's (``acme/finn``, or ``finn`` for any directory)."""
    def named(prefixes: Sequence[str]) -> list[RunView]:
        return [r for r in runs if any(r.id.startswith(p.lower()) for p in prefixes)]

    pool = named(ids) if ids else list(runs)
    if not ids:
        pool = [r for r in pool if _as_matches(r.as_login, as_login)]
    key = set_key or (pool[-1].set_key if pool else "")
    pool = [r for r in pool if r.set_key == key]
    measured = [r for r in pool if r.measured]
    excluded = [r for r in pool if not r.measured]
    # 7D: a candidate model's runs in a trial measure that model, not the build in use
    trials = [] if ids else [r for r in measured if is_trial(r.build)]
    measured = [r for r in measured if r not in trials]
    regressions = [] if ids else [r for r in measured if is_regression(r.build)]
    ordinary = [r for r in measured if r not in regressions]
    baseline = candidate = None
    if baseline_ids or candidate_ids:
        baseline = [r for r in named(baseline_ids) if r.measured and r.set_key == key]
        candidate = [r for r in named(candidate_ids) if r.measured and r.set_key == key]
    elif compare_builds and measured and is_regression(measured[-1].build):
        newest = measured[-1].build
        candidate = [r for r in measured if r.build == newest]
        baseline = [r for r in ordinary if r.build == regressed_build(newest)]
    elif compare_builds:
        builds = list(dict.fromkeys(r.build for r in reversed(ordinary)))
        if len(builds) >= 2:
            candidate = [r for r in ordinary if r.build == builds[0]]
            baseline = [r for r in ordinary if r.build == builds[1]]
        else:  # D98: the build before may have run an earlier version of the set
            everyone = [r for r in runs if r.measured and not is_regression(r.build)
                        and not is_trial(r.build) and _as_matches(r.as_login, as_login)]
            earlier = [r for r in everyone if r.set_key != key and r.build != (
                builds[0] if builds else None)]
            if builds and earlier:
                before = list(dict.fromkeys(r.build for r in reversed(earlier)))[0]
                candidate = [r for r in ordinary if r.build == builds[0]]
                newest_set = next(r.set_key for r in reversed(earlier) if r.build == before)
                baseline = [r for r in earlier if r.build == before
                            and r.set_key == newest_set]
    history: list[RunView] = []
    if baseline and candidate:
        chosen = {r.id: r for r in [*baseline, *candidate]}
        shown = sorted(chosen.values(), key=lambda r: r.started_at)
        regressions = [r for r in regressions if r.id not in chosen]
        first = min(r.started_at for r in candidate)
        history = [r for r in measured if not is_regression(r.build)
                   and r.id not in chosen and r.started_at < first][-HISTORY_RUNS:]
    else:
        baseline = candidate = None
        shown = measured if ids else ordinary[-last:]
    return Selection(key, shown, excluded, baseline, candidate, regressions, history, trials)


def _meanings(r: RunView) -> str:
    if r.meanings_key is None:
        return "unknown"
    n = r.meanings.get("sme_decisions")
    return f"`{r.meanings_key[:6]}`" + (f" ({n} SME)" if n is not None else "")


def meanings_note(baseline: Sequence[RunView], candidate: Sequence[RunView]) -> str | None:
    """Whether the two sets answered with different meanings (D81) — then the difference is
    not the build's alone."""
    b = {r.meanings_key for r in baseline}
    c = {r.meanings_key for r in candidate}
    if None in b or None in c:
        unknown = [r for r in [*baseline, *candidate] if r.meanings_key is None]
        why = ("imported runs" if all(r.mode == "imported" for r in unknown) else
               "recorded before build D81 kept them" if not any(r.mode == "imported"
                                                                for r in unknown)
               else "imported runs, and runs recorded before build D81 kept them")
        return (f"the meanings some of these runs answered with are unknown ({why}): SME "
                "decisions or re-reasoning between them would count as the build's")
    if b != c:
        def decided(runs: Sequence[RunView]) -> str:
            counts = sorted({r.meanings.get("sme_decisions") for r in runs} - {None})
            return "/".join(str(x) for x in counts) or "?"

        return (f"the baseline and the candidate answered with different meanings (SME "
                f"decisions {decided(baseline)} → {decided(candidate)}, or re-reasoning): the "
                "difference is not the build's alone")
    return None


def _explain(detail: dict[str, Any]) -> list[str]:
    """An item's detail in words: whether retrieval or the model missed (D84)."""
    out = []
    missing = detail.get("missing") or []
    if missing:
        out.append(f"the context lacked {', '.join(missing)} (retrieval)")
    elif detail.get("objects"):
        out.append("the context held every object the reference needs (the model's reading)")
    clarified = detail.get("clarification")
    if clarified and len(clarified) > 1 and clarified[1]:
        out.append(f"answered after: “{str(clarified[1])[:200]}”")
    if detail.get("interpretation"):
        out.append(f"Gaia understood: {str(detail['interpretation'])[:240]}")
    for a in (detail.get("assumptions") or [])[:4]:
        out.append(f"assumed: {str(a)[:200]}")
    return [f"* {x}" for x in out]


def wrong_causes(items: Sequence[ItemView]) -> dict[str, Any]:
    """The wrong final answers by cause: an expected object missing from the context
    (retrieval) or all there (the reading)."""
    wrong = [i for i in items if i.correct is False]
    known = [i for i in wrong if i.detail is not None]
    retrieval = [i for i in known if i.detail.get("missing")]
    reading = [i for i in known if not i.detail.get("missing")]
    return {"wrong": len(wrong), "unknown": len(wrong) - len(known),
            "retrieval": len(retrieval), "reading": len(reading),
            "retrieval_questions": sorted({i.key for i in retrieval}),
            "reading_questions": sorted({i.key for i in reading})}


LEVELS = ("high", "medium", "low")


def confidence_levels(items: Sequence[ItemView]) -> dict[str, tuple[int, int]]:
    """Per confidence level shown with a final answer: (right, answered) — answers scored
    against a reference only."""
    out: dict[str, tuple[int, int]] = {}
    for level in LEVELS:
        xs = [i for i in items if i.correct is not None and i.final_status == "answered"
              and ((i.detail or {}).get("confidence") or {}).get("level") == level]
        if xs:
            out[level] = (sum(1 for i in xs if i.correct), len(xs))
    return out


def _per_run(runs: Sequence[RunView]) -> dict[str, float]:
    """Repairs, model calls and tokens per run (a worse prompt may cost before it errs)."""
    n = max(1, len(runs))
    return {"repairs": sum(i.repairs for r in runs for i in r.items.values()) / n,
            "calls": sum(i.calls for r in runs for i in r.items.values()) / n,
            "tokens": sum(i.tokens for r in runs for i in r.items.values()) / n}


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}"


def quality_report(runs: Sequence[RunView], *, data_source: str, question_set_key: str,
                   excluded: Iterable[RunView] = (), baseline: Sequence[RunView] | None = None,
                   candidate: Sequence[RunView] | None = None,
                   regressions: Iterable[RunView] = (),
                   history: Sequence[RunView] = (),
                   trials: Iterable[RunView] = ()) -> tuple[str, dict[str, Any]]:
    """The report over measured runs, and the same numbers as a document."""
    runs = [r for r in runs if r.measured]
    excluded = list(excluded)
    trials = list(trials)
    regressions = list(regressions)
    lines = [f"# Quality — {data_source}", "",
             f"Question set `{question_set_key}` · {len(runs)} measured run(s)"
             + (f" · {len(excluded)} replay(s) left out (they reproduce a run)" if excluded
                else "")
             + (f" · {len(regressions)} deliberate-regression run(s) left out (they measure "
                "the net, not a build)" if regressions else "")
             + (f" · {len(trials)} model-trial run(s) left out (a candidate model's: "
                "make model ARGS=\"show\")" if trials else ""), ""]
    doc: dict[str, Any] = {"data_source": data_source, "question_set_key": question_set_key,
                           "runs": [r.id for r in runs], "excluded": [r.id for r in excluded],
                           "regressions_left_out": [r.id for r in regressions]}
    if not runs:
        lines.append("No measured run of this question set yet: `make golden` with a new "
                     "`--out`, then `make quality` again.")
        return "\n".join(lines) + "\n", doc

    lines += ["## Runs", "",
              "| Run | Started | Build | Model | Mode | As | Meanings | Correct | Executable | "
              "Calls | Tokens |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        scored = [i for i in r.items.values() if i.correct is not None]
        right = sum(1 for i in scored if i.correct)
        ran = sum(1 for i in scored if i.executable)
        lines.append(
            f"| `{r.id[:8]}` | {r.started_at:%Y-%m-%d %H:%M} | {r.build or '—'} | "
            f"{r.model or '—'} | {r.mode} | {r.as_login or '—'} | {_meanings(r)} | "
            f"{right}/{len(scored)} | {ran}/{len(scored)} | "
            f"{sum(i.calls for i in r.items.values())} | "
            f"{sum(i.tokens for i in r.items.values()):,} |")
    for r in excluded:
        lines.append(f"| `{r.id[:8]}` | {r.started_at:%Y-%m-%d %H:%M} | {r.build or '—'} | "
                     f"{r.model or '—'} | replay — left out | {r.as_login or '—'} | "
                     f"{_meanings(r)} | | | | |")

    all_items = [i for r in runs for i in r.items.values()]
    scored = [i for i in all_items if i.correct is not None]
    right = sum(1 for i in scored if i.correct)
    ran = sum(1 for i in scored if i.executable)
    per_run = [sum(1 for i in r.items.values() if i.correct) /
               max(1, sum(1 for i in r.items.values() if i.correct is not None)) for r in runs]
    lines += ["", "## Correct answers over the runs", "",
              f"* correct: {_share(right, len(scored))}",
              f"* executable: {_share(ran, len(scored))}",
              f"* per run: {min(per_run):.0%} to {max(per_run):.0%} "
              f"(mean {sum(per_run) / len(per_run):.0%})"]
    doc["correct"] = {"k": right, "n": len(scored), "interval": wilson(right, len(scored))}
    doc["executable"] = {"k": ran, "n": len(scored), "interval": wilson(ran, len(scored))}
    doc["per_run"] = per_run

    keys = sorted({k for r in runs for k in r.items})
    always, flaky, never = [], [], []
    for k in keys:
        marks = [r.items[k].correct for r in runs if k in r.items and r.items[k].correct
                 is not None]
        if not marks:
            continue
        if all(marks):
            always.append(k)
        elif any(marks):
            flaky.append((k, sum(marks), len(marks)))
        else:
            last = next(r.items[k] for r in reversed(runs) if k in r.items)
            never.append((k, last.reason or "", last.sql))
    lines += ["", "## Questions", "", f"* right in every run: {len(always)}",
              "* right in some runs (flaky): " + (", ".join(f"{k} {a}/{n}" for k, a, n in flaky)
                                                  or "none"),
              "* wrong in every run: " + (", ".join(k for k, _w, _s in never) or "none")]
    details: dict[str, dict[str, Any]] = {}
    for k, why, sql in never:
        if why:
            lines.append(f"  * {k}: {why[:160]}")
        last = next(r.items[k] for r in reversed(runs) if k in r.items)
        if last.detail:  # what the latest answer was made of (D84)
            details[k] = last.detail
            lines += [f"    {x}" for x in _explain(last.detail)]
        if sql:  # the latest answer, to read why (D81)
            lines += ["", "    ```sql", *("    " + x for x in sql.strip().splitlines()),
                      "    ```", ""]
    doc["questions"] = {"always": always, "flaky": [list(x) for x in flaky],
                        "never": [k for k, _w, _s in never],
                        "never_reasons": {k: why for k, why, _s in never if why},
                        "never_sql": {k: sql for k, _w, sql in never if sql},
                        "never_detail": details}

    causes = wrong_causes(all_items)
    if causes["wrong"]:
        lines += ["", "## Why answers were wrong", "",
                  f"* wrong final answers: {causes['wrong']}",
                  f"* the context lacked an object the reference needs (retrieval): "
                  f"{causes['retrieval']}"
                  + (f" — {', '.join(causes['retrieval_questions'])}"
                     if causes["retrieval_questions"] else ""),
                  f"* the context held them all (the model's reading, or the question's): "
                  f"{causes['reading']}"
                  + (f" — {', '.join(causes['reading_questions'])}"
                     if causes["reading_questions"] else "")]
        if causes["unknown"]:
            lines.append(f"* not known (runs recorded before build D84): {causes['unknown']}")
    doc["wrong_causes"] = causes

    levels = confidence_levels(all_items)
    if levels:
        lines += ["", "## The confidence Gaia showed, against the reference (6D)", ""]
        for level, (k, n) in levels.items():
            lines.append(f"* shown as {level}: right {_share(k, n)}")
        unknown = sum(1 for i in all_items if i.correct is not None
                      and i.final_status == "answered"
                      and not ((i.detail or {}).get("confidence") or {}).get("level"))
        if unknown:
            lines.append(f"* not known: {unknown} answer(s) (runs recorded before build D84 kept "
                         "no confidence)")
        lines.append("* \"high\" should be right at least 95% of the time — the interval's lower "
                     "bound says whether it is (P6-2)")
        doc["confidence_levels"] = {lv: list(kn) for lv, kn in levels.items()}

    clarify = [i for i in all_items if i.expect == "clarify"]
    asked = sum(1 for i in clarify if i.first_status in ("clarify", "cannot_answer"))
    answer = [i for i in all_items if i.expect == "answer"]
    over = sum(1 for i in answer if i.over_clarified)
    guessed = [i for i in clarify if i.best_guess in ("correct", "wrong", "invalid", "none")
               and i.first_status == "clarify" and i.correct is not None]
    agree = sum(1 for i in guessed if i.best_guess == "correct")
    lines += ["", "## Clarification", "",
              f"* asked back when the question needed it: {_share(asked, len(clarify))}",
              f"* asked back although the question was clear: {over} of {len(answer)}",
              f"* Gaia's best guess was the user's answer: {_share(agree, len(guessed))} — "
              "answering without asking needs the interval's lower bound above 90% (D61)"]
    doc["clarification"] = {"asked": [asked, len(clarify)], "over": [over, len(answer)],
                            "agree": [agree, len(guessed)]}

    secs = sorted(i.seconds for i in all_items if i.seconds)
    if secs:
        p95 = secs[min(len(secs) - 1, math.ceil(0.95 * len(secs)) - 1)]
        lines += ["", "## Cost and time", "",
                  f"* per run: {sum(i.calls for i in all_items) / len(runs):.0f} model calls, "
                  f"{sum(i.tokens for i in all_items) / len(runs):,.0f} tokens",
                  f"* per question: {median(secs):.1f} s median, {p95:.1f} s at the 95th "
                  "percentile"]

    if baseline and candidate:
        c = compare(baseline, candidate, history=history)
        word = {"passes": "**passes**",
                "fails": "**fails**" + (f" — broken: {', '.join(c.breaks)}" if c.breaks else ""),
                "cannot tell": "**cannot tell** — no question broke, but the overall difference "
                               "cannot rule out a loss of 3 points (each of 30 questions is 3.3)"
                }[c.verdict]
        lines += ["", "## Candidate against baseline (P6-2, P6-8)", "",
                  f"* baseline: {', '.join(f'`{r.id[:8]}`' for r in baseline)} "
                  f"({', '.join(sorted({r.build or '—' for r in baseline}))})",
                  f"* candidate: {', '.join(f'`{r.id[:8]}`' for r in candidate)} "
                  f"({', '.join(sorted({r.build or '—' for r in candidate}))})",
                  "* named breaks (right in every baseline run, wrong in every candidate run): "
                  + (", ".join(c.breaks) if c.breaks else
                     "none" if min(len(baseline), len(candidate)) >= MIN_RUNS_FOR_BREAKS else
                     f"not looked for (fewer than {MIN_RUNS_FOR_BREAKS} runs a side)")
                  + (f"; fixed: {', '.join(c.fixes)}" if c.fixes else ""),
                  *(["* flipped, but known to be flaky (right and wrong within one build: "
                     + "; ".join(f"{k} in {', '.join(sorted(set(b)))}"
                                 for k, b in sorted(c.flaky_flips.items()))
                     + ") — not named: chance moves them"] if c.flaky_flips else []),
                  f"* correct, question by question: {_pct(c.difference)} points (95% interval "
                  f"{_pct(c.low)} to {_pct(c.high)}, runs and questions resampled) over "
                  f"{c.questions} question(s)"
                  + (f" both sides share — {c.only_one_side} on one side only"
                     if c.only_one_side else "")
                  + (f"; left out, worded differently on the two sides: "
                     f"{', '.join(c.reworded)}" if c.reworded else ""),
                  f"* verdict: {word}"]
        if c.gap_hours is not None and c.gap_hours > SESSION_HOURS:
            lines.append(f"* the baseline's last run was made {c.gap_hours:.0f} h before the "
                         "candidate's first: the provider may have changed its model in between "
                         "— for a verdict about the build alone, run the baseline again in the "
                         "same session")
        expected = sorted({k for r in candidate for k in (r.expected_breaks or [])})
        if any(is_regression(r.build) for r in candidate):
            lines.append("* the candidate is a **deliberate regression** (6E): the net catches "
                         "it only if the verdict is *fails* — *passes* or *cannot tell* means "
                         "these runs did not see it")
            if expected:
                caught = [k for k in expected if k in c.breaks]
                lines.append(f"  * expected to break (the questions that need what it took "
                             f"away): {', '.join(expected)} — named: {len(caught)} of "
                             f"{len(expected)}"
                             + (f"; named but not expected: "
                                f"{', '.join(k for k in c.breaks if k not in expected)}"
                                if any(k not in expected for k in c.breaks) else ""))
        note = meanings_note(baseline, candidate)
        if note:
            lines.append(f"* **{note}**")
            doc_note = note
        else:
            doc_note = None
        for k, b, cand in c.moved:
            lines.append(f"  * {k}: right in {b:.0%} of the baseline's runs, {cand:.0%} of the "
                         "candidate's")
        cost = {side: _per_run(runs_) for side, runs_ in (("baseline", baseline),
                                                           ("candidate", candidate))}
        lines.append(f"* per run: repairs {cost['baseline']['repairs']:.1f} → "
                     f"{cost['candidate']['repairs']:.1f}, model calls "
                     f"{cost['baseline']['calls']:.0f} → {cost['candidate']['calls']:.0f}, "
                     f"tokens {cost['baseline']['tokens']:,.0f} → "
                     f"{cost['candidate']['tokens']:,.0f}")
        doc["comparison"] = {"difference": c.difference, "low": c.low, "high": c.high,
                             "questions": c.questions, "verdict": c.verdict,
                             "moved": [list(m) for m in c.moved], "meanings": doc_note,
                             "per_run": cost, "breaks": c.breaks, "fixes": c.fixes,
                             "gap_hours": c.gap_hours, "expected_breaks": expected or None,
                             "flaky_flips": c.flaky_flips, "reworded": c.reworded,
                             "only_one_side": c.only_one_side}
    return "\n".join(lines) + "\n", doc
