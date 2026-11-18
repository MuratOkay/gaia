"""The quality report (Phase 6A, D80): golden runs as measurements, with their uncertainty.

One golden run of 30 questions cannot tell a regression of a few points from the model's
run-to-run variation: the D72 prompt gave 97, 90 and 93 % on three fresh runs (OWNER_VERIFICATION
§26c F6). So the report pools **several runs**, gives every proportion a 95 % **Wilson**
interval, shows which questions are right always, sometimes ("flaky") or never, and compares two
sets of runs **question by question**: the mean of the per-question differences, with a 95 %
interval from a seeded **paired bootstrap** over the questions. P6-2: a candidate passes when the
lower bound of that difference is above −3 points; fails when the upper bound is below it; else
the runs cannot tell, and more are needed.

Replays are never measurements (they reproduce a run); they are listed and left out.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

__all__ = ["Comparison", "ItemView", "RunView", "Selection", "compare", "confidence_levels",
           "quality_report", "select_runs", "wilson", "wrong_causes"]

THRESHOLD = -0.03  # P6-2: a change may not cost more than 3 points (lower bound)
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


def compare(baseline: Sequence[RunView], candidate: Sequence[RunView], *,
            iterations: int = 4000, seed: int = 7, threshold: float = THRESHOLD) -> Comparison:
    keys = sorted(set().union(*(r.items for r in baseline)) & set().union(
        *(r.items for r in candidate)))
    pairs = [(k, b, c) for k in keys
             if (b := _rate(baseline, k)) is not None and (c := _rate(candidate, k)) is not None]
    if not pairs:
        return Comparison(0.0, 0.0, 0.0, 0, "cannot tell", [])
    diffs = [c - b for _k, b, c in pairs]
    mean = sum(diffs) / len(diffs)
    rng = random.Random(seed)  # noqa: S311 - a reproducible bootstrap, not a secret
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs)
                   for _ in range(iterations))
    low, high = boots[int(0.025 * iterations)], boots[int(0.975 * iterations) - 1]
    verdict = "passes" if low > threshold else "fails" if high < threshold else "cannot tell"
    moved = [(k, b, c) for k, b, c in pairs if abs(c - b) >= 0.5]
    return Comparison(mean, low, high, len(pairs), verdict, moved)


@dataclass
class Selection:
    set_key: str
    shown: list[RunView]
    excluded: list[RunView]
    baseline: list[RunView] | None = None
    candidate: list[RunView] | None = None


def select_runs(runs: Sequence[RunView], *, ids: Sequence[str] = (), set_key: str | None = None,
                last: int = 3, baseline_ids: Sequence[str] = (),
                candidate_ids: Sequence[str] = (), compare_builds: bool = False) -> Selection:
    """Which runs a report reads: one question set (the newest run's unless given); the last
    ``last`` measured runs, or the runs named; for a comparison, the baseline and candidate
    named — or, with ``compare_builds``, the newest build's runs against the build before."""
    def named(prefixes: Sequence[str]) -> list[RunView]:
        return [r for r in runs if any(r.id.startswith(p.lower()) for p in prefixes)]

    pool = named(ids) if ids else list(runs)
    key = set_key or (pool[-1].set_key if pool else "")
    pool = [r for r in pool if r.set_key == key]
    measured = [r for r in pool if r.measured]
    excluded = [r for r in pool if not r.measured]
    baseline = candidate = None
    if baseline_ids or candidate_ids:
        baseline = [r for r in named(baseline_ids) if r.measured and r.set_key == key]
        candidate = [r for r in named(candidate_ids) if r.measured and r.set_key == key]
    elif compare_builds:
        builds = list(dict.fromkeys(r.build for r in reversed(measured)))
        if len(builds) >= 2:
            candidate = [r for r in measured if r.build == builds[0]]
            baseline = [r for r in measured if r.build == builds[1]]
    if baseline and candidate:
        chosen = {r.id: r for r in [*baseline, *candidate]}
        shown = sorted(chosen.values(), key=lambda r: r.started_at)
    else:
        baseline = candidate = None
        shown = measured if ids else measured[-last:]
    return Selection(key, shown, excluded, baseline, candidate)


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
                   candidate: Sequence[RunView] | None = None) -> tuple[str, dict[str, Any]]:
    """The report over measured runs, and the same numbers as a document."""
    runs = [r for r in runs if r.measured]
    excluded = list(excluded)
    lines = [f"# Quality — {data_source}", "",
             f"Question set `{question_set_key}` · {len(runs)} measured run(s)"
             + (f" · {len(excluded)} replay(s) left out (they reproduce a run)" if excluded
                else ""), ""]
    doc: dict[str, Any] = {"data_source": data_source, "question_set_key": question_set_key,
                           "runs": [r.id for r in runs], "excluded": [r.id for r in excluded]}
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
        c = compare(baseline, candidate)
        word = {"passes": "**passes**", "fails": "**fails**",
                "cannot tell": "**cannot tell** — more runs are needed"}[c.verdict]
        lines += ["", "## Candidate against baseline (P6-2)", "",
                  f"* baseline: {', '.join(f'`{r.id[:8]}`' for r in baseline)} "
                  f"({', '.join(sorted({r.build or '—' for r in baseline}))})",
                  f"* candidate: {', '.join(f'`{r.id[:8]}`' for r in candidate)} "
                  f"({', '.join(sorted({r.build or '—' for r in candidate}))})",
                  f"* correct, question by question: {_pct(c.difference)} points (95% interval "
                  f"{_pct(c.low)} to {_pct(c.high)}) over {c.questions} question(s)",
                  f"* verdict: {word} (a change may cost at most 3 points)"]
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
                             "per_run": cost}
    return "\n".join(lines) + "\n", doc
