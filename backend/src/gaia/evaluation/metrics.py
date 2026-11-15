"""Accuracy and score informativeness (PHASES, 3C acceptance).

* **meaning accuracy** = (yes + ½ · partial) / judged columns;
* **role accuracy** = share of columns whose role is one the reference allows
  (only where a label or ground truth gives roles);
* **AUROC** of the score against "meaning = yes": the probability that a
  correct meaning scores above an incorrect one (ties count ½). 0.5 is a coin,
  1.0 a perfect ranking. Undefined when every column is correct or none is;
* **precision at accept_at**: of the meanings the machine would accept, how many
  are right — the number an SME most needs to trust the automation.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["CREDIT", "auroc", "meaning_accuracy", "precision_at"]

CREDIT = {"yes": 1.0, "partial": 0.5, "no": 0.0}


def meaning_accuracy(verdicts: Sequence[str]) -> float | None:
    if not verdicts:
        return None
    return sum(CREDIT.get(v, 0.0) for v in verdicts) / len(verdicts)


def auroc(scores: Sequence[float], correct: Sequence[bool]) -> float | None:
    positives = [s for s, c in zip(scores, correct, strict=True) if c]
    negatives = [s for s, c in zip(scores, correct, strict=True) if not c]
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def precision_at(
    scores: Sequence[float], correct: Sequence[bool], threshold: float
) -> tuple[float | None, int]:
    picked = [c for s, c in zip(scores, correct, strict=True) if s >= threshold]
    if not picked:
        return None, 0
    return sum(picked) / len(picked), len(picked)
