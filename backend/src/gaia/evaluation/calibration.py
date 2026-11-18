"""Scores that mean what they say (Phase 6C, D86; answers: 6D, D87).

A raw score ranks; it does not promise. A **calibrator** maps it to P(right), fitted on labels
from people (6B) for one model and one scoring version — a score's meaning changes with either.

* **Which method** (P6-1): isotonic regression from ``isotonic_min_labels`` labels with at least
  ``isotonic_min_wrong`` not right (wrong or partly); Platt scaling (a logistic curve: two
  numbers, safer on fewer labels) from ``platt_min_labels``/``platt_min_wrong``; below that the
  **identity**, and the screens say "uncalibrated". Since D89 the minimums count **subjects**
  (a column, a golden question), not labels: repeated runs of 30 questions are not 180 pieces
  of evidence.
* **Judged on labels it did not see**: the labels are split into ``folds`` (by the column, or the
  question, so one subject never sits on both sides); each fold is predicted by a calibrator
  fitted on the others. On those held-out predictions, against the identity: the **expected
  calibration error** (ECE — per band of predicted probability, how far the share right is from
  what was predicted, weighted by the band's labels), the **Brier score** (the mean squared
  distance between the prediction and the verdict; lower is better) and the **reliability
  diagram** (predicted against observed, per band). A calibrator that does not beat the
  identity on both is not put in use unless forced.
* **A lower bound** beside every probability: the Wilson bound of the labels behind it —
  isotonic: the labels of the step the score falls on; Platt: the labels within the score's
  tenth of the scale. P6-3 accepts a meaning when P(right) ≥ ``accept_p`` and the bound ≥
  ``accept_lower``: a probability from few labels cannot accept anything.

A label's value is 1 for right, ½ for partly, 0 for wrong (as in the reports). Everything here
is deterministic: the same labels give the same calibrator, version and metrics.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Fit", "Point", "Trial", "choose_method", "cross_validated", "fit", "identity",
           "labels_key", "metrics", "subjects", "trial", "wilson_lower"]

Z = 1.959964


@dataclass(frozen=True)
class Point:
    """One label: the raw score Gaia gave, the person's verdict as a value, and the subject
    (a column, a question) that keeps its labels in one fold."""

    score: float
    y: float  # 1 right, 0.5 partly, 0 wrong
    subject: str


def wilson_lower(p: float, n: float) -> float:
    """The 95 % Wilson lower bound of a share ``p`` observed over ``n`` labels."""
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, p))
    d = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / d
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return max(0.0, centre - half)


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


@dataclass
class Fit:
    """A fitted map from raw score to P(right). ``params``: isotonic ``blocks`` — [from, to,
    P, labels] per step; Platt ``a``, ``b`` and ``support`` — labels per tenth of the scale."""

    method: str  # identity | platt | isotonic
    params: dict[str, Any] = field(default_factory=dict)
    version: str = "identity"

    @property
    def calibrated(self) -> bool:
        return self.method != "identity"

    def p(self, raw: float) -> float:
        raw = _clamp(raw)
        if self.method == "platt":
            z = self.params["a"] * raw + self.params["b"]
            return round(1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z)))), 6)
        if self.method == "isotonic":
            blocks = self.params["blocks"]
            if raw <= blocks[0][0]:
                return round(blocks[0][2], 6)
            for lo, hi, p, _n in blocks:
                if lo <= raw <= hi:
                    return round(p, 6)
            for (_lo0, hi0, p0, _n0), (lo1, _hi1, p1, _n1) in zip(blocks, blocks[1:],
                                                                   strict=False):
                if hi0 < raw < lo1:  # between two steps: the straight line between them
                    return round(p0 + (p1 - p0) * (raw - hi0) / (lo1 - hi0), 6)
            return round(blocks[-1][2], 6)
        return round(raw, 6)

    def support(self, raw: float) -> float:
        """How many labels stand behind the probability at ``raw``."""
        raw = _clamp(raw)
        if self.method == "isotonic":
            blocks = self.params["blocks"]
            best = min(blocks, key=lambda b: 0.0 if b[0] <= raw <= b[1]
                       else min(abs(raw - b[0]), abs(raw - b[1])))
            return float(best[3])
        if self.method == "platt":
            counts = self.params.get("support") or []
            return float(counts[min(len(counts) - 1, int(raw * len(counts)))]) if counts else 0.0
        return 0.0

    def lower(self, raw: float) -> float:
        """The probability's 95 % lower bound (the identity has none: 0)."""
        if not self.calibrated:
            return 0.0
        return round(wilson_lower(self.p(raw), self.support(raw)), 6)

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "params": self.params, "version": self.version}


def identity() -> Fit:
    return Fit("identity", {}, "identity")


# -- fitting ----------------------------------------------------------------------------------


def _pav(points: Sequence[Point]) -> list[list[float]]:
    """Pool adjacent violators: the non-decreasing step function closest to the labels (least
    squares). Labels of one score are pooled first. Returns [from, to, P, labels] per step."""
    by_score: dict[float, list[float]] = {}
    for p in points:
        by_score.setdefault(round(p.score, 6), []).append(p.y)
    blocks: list[list[float]] = []  # from, to, sum, n
    for score in sorted(by_score):
        ys = by_score[score]
        blocks.append([score, score, sum(ys), float(len(ys))])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] >= blocks[-1][2] / blocks[-1][3]:
            last = blocks.pop()
            blocks[-1][1] = last[1]
            blocks[-1][2] += last[2]
            blocks[-1][3] += last[3]
    return [[lo, hi, round(total / n, 6), int(n)] for lo, hi, total, n in blocks]


def _platt(points: Sequence[Point], ridge: float = 1e-3) -> tuple[float, float]:
    """The logistic curve P = 1 / (1 + exp(−(a·score + b))) of the labels, by Newton's method
    with a small ridge (no infinite slope on separable labels). Fractional labels (partly)
    are fine: the log-likelihood is the same sum."""
    a, b = 1.0, 0.0
    for _ in range(100):
        ga = gb = haa = hab = hbb = 0.0
        for p in points:
            z = a * p.score + b
            q = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
            r = q - p.y
            w = q * (1 - q)
            ga += r * p.score
            gb += r
            haa += w * p.score * p.score
            hab += w * p.score
            hbb += w
        ga += ridge * a
        haa += ridge
        hbb += 1e-9
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return round(a, 6), round(b, 6)


def labels_key(points: Sequence[Point]) -> str:
    text = "\n".join(sorted(f"{p.subject}|{p.score:.6f}|{p.y}" for p in points))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def fit(points: Sequence[Point], method: str) -> Fit:
    key = labels_key(points)
    if method == "isotonic" and points:
        return Fit("isotonic", {"blocks": _pav(points)}, f"isotonic-{key[:10]}")
    if method == "platt" and points:
        a, b = _platt(points)
        support = [0] * 10
        for p in points:
            support[min(9, int(_clamp(p.score) * 10))] += 1
        return Fit("platt", {"a": a, "b": b, "support": support}, f"platt-{key[:10]}")
    return identity()


def not_right(points: Sequence[Point]) -> int:
    return sum(1 for p in points if p.y < 1.0)


def subjects(points: Sequence[Point]) -> tuple[int, int]:
    """(subjects, subjects not always right): the independent evidence behind the labels. Six
    golden runs of one question are six labels but one question — its repeats say how often
    the model errs on it, not on another (D89, OWNER_VERIFICATION §30)."""
    seen = {p.subject for p in points}
    wrong = {p.subject for p in points if p.y < 1.0}
    return len(seen), len(wrong)


def choose_method(points: Sequence[Point], cfg) -> tuple[str, str]:
    """(method, why) by P6-1's minimums (``cfg``: the ``calibration`` settings). The minimums
    count **subjects** — columns, questions — not labels: a subject labelled again (a golden
    question in every run) counts once, and as not right if any of its labels is not."""
    labels = len(points)
    n, wrong = subjects(points)
    what = (f"{n} label(s), {wrong} not right" if n == labels else
            f"{labels} labels on {n} subject(s) (columns or questions, each counted once), "
            f"{wrong} of them not always right")
    if n >= cfg.isotonic_min_labels and wrong >= cfg.isotonic_min_wrong:
        return "isotonic", f"{what}: enough for isotonic regression"
    if n >= cfg.platt_min_labels and wrong >= cfg.platt_min_wrong:
        return "platt", (f"{what}: Platt scaling (isotonic needs "
                         f"{cfg.isotonic_min_labels} with {cfg.isotonic_min_wrong} not right)")
    return "identity", (f"{what}: too few to calibrate (Platt needs "
                        f"{cfg.platt_min_labels} with {cfg.platt_min_wrong} not right, "
                        f"isotonic {cfg.isotonic_min_labels} with {cfg.isotonic_min_wrong})")


def _fold(subject: str, folds: int) -> int:
    return int(hashlib.sha256(subject.encode()).hexdigest()[:8], 16) % folds


def cross_validated(points: Sequence[Point], method: str, folds: int) -> list[float]:
    """Each label's probability from a calibrator fitted without its fold."""
    out = [0.0] * len(points)
    assignment = [_fold(p.subject, folds) for p in points]
    for k in range(folds):
        train = [p for p, f in zip(points, assignment, strict=True) if f != k]
        model = fit(train, method) if train else identity()
        for i, (p, f) in enumerate(zip(points, assignment, strict=True)):
            if f == k:
                out[i] = model.p(p.score)
    return out


def metrics(predicted: Sequence[float], points: Sequence[Point], bins: int) -> dict[str, Any]:
    """ECE, Brier and the reliability diagram of predictions against the labels."""
    n = len(points)
    if n == 0:
        return {"n": 0, "ece": None, "brier": None, "reliability": []}
    brier = sum((q - p.y) ** 2 for q, p in zip(predicted, points, strict=True)) / n
    rows, ece = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(q, p.y) for q, p in zip(predicted, points, strict=True)
                   if lo <= q < hi or (b == bins - 1 and q == 1.0)]
        if not members:
            continue
        mean_q = sum(q for q, _y in members) / len(members)
        mean_y = sum(y for _q, y in members) / len(members)
        ece += len(members) / n * abs(mean_q - mean_y)
        rows.append({"from": round(lo, 3), "to": round(hi, 3), "n": len(members),
                     "predicted": round(mean_q, 4), "observed": round(mean_y, 4)})
    return {"n": n, "ece": round(ece, 4), "brier": round(brier, 4), "reliability": rows}


@dataclass
class Trial:
    """A calibrator fitted and judged: what ``make calibrate`` reports and may put in use."""

    method: str
    why: str
    labels: int
    wrong: int
    key: str
    fit: Fit
    before: dict[str, Any]  # the identity, on every label
    held_out: dict[str, Any] | None  # the calibrator, on labels it was not fitted on
    subjects: tuple[int, int] = (0, 0)  # subjects, subjects not always right (P6-1 counts these)

    @property
    def improves(self) -> bool:
        if self.held_out is None or self.before["brier"] is None:
            return False
        return (self.held_out["brier"] < self.before["brier"]
                and self.held_out["ece"] < self.before["ece"])

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "why": self.why, "labels": self.labels,
                "wrong": self.wrong, "subjects": self.subjects[0],
                "subjects_wrong": self.subjects[1], "key": self.key, "version": self.fit.version,
                "improves": self.improves, "identity": self.before,
                "held_out": self.held_out}


def trial(points: Sequence[Point], cfg, method: str | None = None) -> Trial:
    """Choose the method (or take ``method``), judge it on held-out labels against the
    identity, and fit it on every label."""
    chosen, why = choose_method(points, cfg)
    if method and method != chosen:
        why = f"{method} as asked ({why})"
        chosen = method
    before = metrics([_clamp(p.score) for p in points], points, cfg.bins)
    held = None
    model = identity()
    if chosen != "identity" and points:
        held = metrics(cross_validated(points, chosen, max(2, cfg.folds)), points, cfg.bins)
        model = fit(points, chosen)
    return Trial(chosen, why, len(points), not_right(points), labels_key(points), model,
                 before, held, subjects(points))
