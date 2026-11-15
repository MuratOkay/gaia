"""The SME review queue (Phase 3B, Stage 4).

A column goes to the queue when the machine has run out of means, or when its
meaning must not be presented as fact:

* ``abstain`` — score below ``review_required_below``: queued at once;
* ``evidence_exhausted`` — below ``accept_at`` and no evidence stage is left
  (``evidence.next_stage`` is None): more automation will not help.

Columns that can still gain evidence (profiling is available but has not run)
are *not* queued: an SME's time is the scarcest resource (premise A1). Unused
columns (empty or constant) are never queued.

Extra tags explain the item: ``contradiction`` (a contradicting evidence item
of strength >= 0.5), ``critique_disagrees`` (the critique said doubtful or
contradicted) and ``test_not_run`` (a Stage 3 test of the claim could not run:
budget, timeout, or a target that no longer exists).

``priority = (1 - score) x importance``, where importance (0.05..1) reflects how
much a wrong meaning would cost: table size, foreign-key degree, and whether
the column is a key/indexed or a foreign key.
"""

from __future__ import annotations

import math

from gaia.core.config import ProfilingConfig
from gaia.semantics.bundle import ColumnFacts
from gaia.semantics.evidence import Ledger
from gaia.semantics.schemas import ColumnCritique
from gaia.semantics.scoring import BAND_ACCEPTED, ColumnScore

__all__ = ["importance", "review_reasons"]


def importance(facts: ColumnFacts, table_rows: int | None, fk_degree: int) -> float:
    rows = min(1.0, math.log10((table_rows or 0) + 1) / 8)  # 100 M rows -> 1
    degree = min(1.0, fk_degree / 6)
    indexed = any(r.startswith(("pk:", "uq:", "ix:")) for r in facts.refs)
    value = 0.4 * rows + 0.3 * degree + 0.2 * indexed + 0.1 * bool(facts.foreign_keys)
    return round(max(0.05, value), 4)


def review_reasons(
    score: ColumnScore,
    *,
    ledger: Ledger | None,
    critique: ColumnCritique | None,
    exhausted: bool,
    unused: bool,
    bands: ProfilingConfig,
) -> list[str]:
    """Why this column needs an SME; empty when it does not (yet)."""
    if unused or score.band == BAND_ACCEPTED:
        return []
    if score.calibrated_score < bands.review_required_below:
        reasons = ["abstain"]
    elif exhausted:
        reasons = ["evidence_exhausted"]
    else:
        return []
    if ledger is not None and any(i.strength >= 0.5 for i in ledger.contradicting):
        reasons.append("contradiction")
    if critique is not None and critique.verdict in ("doubtful", "contradicted"):
        reasons.append("critique_disagrees")
    if ledger is not None and ledger.inconclusive:
        reasons.append("test_not_run")
    return reasons
